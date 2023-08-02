import json
import os
from unittest.mock import MagicMock, patch

import pytest

from deploy_recipe import main


@pytest.fixture(params=["", "abcdefg"])
def head_ref(request):
    return request.param


@pytest.fixture(params=["", "true"])
def select_recipe_by_label(request):
    return request.param


@pytest.fixture(params=["string", "file"])
def pangeo_forge_runner_config(request, tmp_path_factory):
    config_string = '{"a": "b"}'
    if request.param == "file":
        fn = tmp_path_factory.mktemp("config") / "pangeo-forge-runner-config.json"
        resolved_path = fn.resolve()
        with open(resolved_path, mode="w") as f:
            json.dump(config_string, f)
        return str(resolved_path)
    else:
        return config_string


@pytest.fixture
def env(select_recipe_by_label, head_ref, pangeo_forge_runner_config):
    return {
        "CONDA_ENV": "notebook",
        "GITHUB_REPOSITORY": "my/repo",
        "GITHUB_API_URL": "https://api.github.com",
        # fixturing of `head_ref` reflects that on `push`
        # events, `GITHUB_HEAD_REF` env var is empty
        "GITHUB_HEAD_REF": head_ref,
        "GITHUB_SHA": "gfedcba",
        "GITHUB_REPOSITORY_ID": "1234567890",
        "GITHUB_RUN_ID": "0987654321",
        "GITHUB_RUN_ATTEMPT": "1",
        # TODO: parametrize runner config with `BaseCommand.feedstock-subdir`
        "INPUT_PANGEO_FORGE_RUNNER_CONFIG": pangeo_forge_runner_config,
        "INPUT_SELECT_RECIPE_BY_LABEL": select_recipe_by_label,
    }


@pytest.fixture
def requests_get_returns_json():
    return [
        {"labels": [{"name": "run:my-recipe"}]}
    ]


@pytest.fixture
def subprocess_return_values():
    return dict(
        stdout='{"job_id": "foo", "job_name": "bar"}',
        stderr="",
        returncode=0,
    )


@pytest.fixture(
    params=[
        ["meta.yaml", "recipe.py"],
        ["meta.yaml", "recipe.py", "requirements.txt"]
    ]
)
def listdir_return_value(request):
    return request.param


@pytest.fixture
def mock_tempfile_name():
    return "mock-temp-file.json"


@patch("deploy_recipe.os.listdir")
@patch("deploy_recipe.subprocess.run")
@patch("deploy_recipe.requests.get")
@patch("deploy_recipe.tempfile.NamedTemporaryFile")
def test_main(
    # Note that patches are passed to `test_main` by order, not name. 
    # See https://docs.python.org/3/library/unittest.mock.html#quick-guide:
    # "When you nest patch decorators the mocks are passed in to the decorated
    # function in the same order they applied (the normal Python order that
    # decorators are applied). This means from the bottom up..."
    named_temporary_file: MagicMock,
    requests_get: MagicMock,
    subprocess_run: MagicMock,
    listdir: MagicMock,
    env: dict,
    requests_get_returns_json: list,
    subprocess_return_values: dict,
    listdir_return_value: list,
    mock_tempfile_name: str,
):  
    # mock a context manager, see: https://stackoverflow.com/a/28852060
    named_temporary_file.return_value.__enter__.return_value.name = mock_tempfile_name

    # mock reponse of requests.get call to github api 
    requests_get.return_value.json.return_value = requests_get_returns_json

    # mock result of subprocess call to `pangeo-forge-runner`
    subprocess_run.return_value.stdout.decode.return_value = subprocess_return_values["stdout"]
    subprocess_run.return_value.stderr.decode.return_value = subprocess_return_values["stderr"]
    subprocess_run.return_value.returncode = subprocess_return_values["returncode"]

    # mock listdir call return value
    listdir.return_value = listdir_return_value

    with patch.dict(os.environ, env):
        main()

        if "requirements.txt" in listdir_return_value:
            to_install = "feedstock/requirements.txt"  # TODO: parametrize
            subprocess_run.assert_any_call(
                f"mamba run -n {env['CONDA_ENV']} pip install -Ur {to_install}".split(),
                capture_output=True,
                text=True,
            )
        else:
            # if 'requirements.txt' not present, 'pip' is never invoked. re: `call_args_list`, see:
            # https://docs.python.org/3/library/unittest.mock.html#unittest.mock.Mock.call_args_list
            for call in [args[0][0] for args in subprocess_run.call_args_list]:
                assert "pip" not in call

        listdir.assert_called_once()

        if env["INPUT_SELECT_RECIPE_BY_LABEL"]:
            # requests.get is always called if INPUT_SELECT_RECIPE_BY_LABEL=true (to check github
            # api for PR `run:...` labels). if this is a `pull_request` event, the called gh api
            # url should contain the `GITHUB_HEAD_REF` for that PR. if this is a `push` event
            # (reflected in the env by the fact that `GITHUB_HEAD_REF` is empty), then we expect to
            # be calling an api url for the `GITHUB_SHA` associated with the `push`.
            called_gh_api_url = requests_get.call_args[0][0]
            if env["GITHUB_HEAD_REF"]:
                assert env["GITHUB_HEAD_REF"] in called_gh_api_url
            else:
                assert env["GITHUB_SHA"] in called_gh_api_url
            
            run_labels = [
                label["name"].split("run:")[-1]
                for pull_request in requests_get_returns_json
                for label in pull_request["labels"]
                if label["name"].startswith("run:")
            ]
            if run_labels:
                subprocess_run.assert_called_with(
                    [
                        'pangeo-forge-runner',
                        'bake',
                        '--repo=.',
                        '--json',
                        f'-f={mock_tempfile_name}',
                        f'--Bake.recipe_id={run_labels[-1]}',
                        f'--Bake.job_name=my-recipe-1234567890-0987654321-1'
                    ],
                    capture_output=True,
                )

        else:
            # subprocess.run is always called if INPUT_SELECT_RECIPE_BY_LABEL is empty
            subprocess_run.assert_called()

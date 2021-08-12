"""Tests for runway.cfngin.hooks.aws_lambda."""
# pylint: disable=invalid-name,no-self-use
# pyright: basic, reportUnknownArgumentType=none, reportUnknownVariableType=none
# pyright: reportFunctionMemberAccess=none, reportOptionalMemberAccess=none
# pyright: reportOptionalOperand=none
from __future__ import annotations

import logging
import os
import os.path
import random  # pylint: disable=syntax-error
import sys
import unittest
from io import BytesIO as StringIO
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple, Union, cast
from zipfile import ZipFile

import boto3
import pytest
from botocore.exceptions import ClientError
from mock import ANY, MagicMock, patch
from moto import mock_s3
from testfixtures import ShouldRaise, TempDirectory, compare
from troposphere.awslambda import Code

from runway.cfngin.exceptions import InvalidDockerizePipConfiguration
from runway.cfngin.hooks.aws_lambda import (
    ZIP_PERMS_MASK,
    _calculate_hash,
    copydir,
    dockerized_pip,
    find_requirements,
    handle_requirements,
    select_bucket_region,
    should_use_docker,
    upload_lambda_functions,
)
from runway.config import CfnginConfig
from runway.context import CfnginContext

from ...mock_docker.fake_api import FAKE_CONTAINER_ID, FAKE_IMAGE_ID
from ...mock_docker.fake_api_client import make_fake_client
from ..factories import mock_provider

if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client
    from pytest import LogCaptureFixture, MonkeyPatch

REGION = "us-east-1"
ALL_FILES = (
    "f1/f1.py",
    "f1/f1.pyc",
    "f1/__init__.py",
    "f1/test/__init__.py",
    "f1/test/f1.py",
    "f1/test/f1.pyc",
    "f1/test2/test.txt",
    "f2/f2.js",
)
F1_FILES = [p[3:] for p in ALL_FILES if p.startswith("f1")]
F2_FILES = [p[3:] for p in ALL_FILES if p.startswith("f2")]


class TestLambdaHooks(unittest.TestCase):
    """Tests for runway.cfngin.hooks.aws_lambda."""

    _s3 = None

    @classmethod
    def temp_directory_with_files(
        cls, files: Union[List[str], Tuple[str, ...]] = ALL_FILES
    ) -> TempDirectory:
        """Create a temp directory with files."""
        temp_dict = TempDirectory()
        for file_ in files:
            temp_dict.write(file_, b"")
        return temp_dict

    @property
    def s3(self) -> S3Client:  # pylint: disable=invalid-name
        """Return S3 client."""
        if not self._s3:
            self._s3 = boto3.client("s3", region_name=REGION)
        return self._s3

    def assert_s3_zip_file_list(self, bucket: str, key: str, files: List[str]) -> None:
        """Assert s3 zip file list."""
        object_info = self.s3.get_object(Bucket=bucket, Key=key)
        zip_data = StringIO(object_info["Body"].read())

        found_files = set()
        with ZipFile(zip_data, "r") as zip_file:
            for zip_info in zip_file.infolist():
                perms = (zip_info.external_attr & ZIP_PERMS_MASK) >> 16
                self.assertIn(
                    perms, (0o755, 0o644), "ZIP member permission must be 755 or 644"
                )
                found_files.add(zip_info.filename)

        compare(found_files, set(files))

    def assert_s3_bucket(self, bucket: str, present: bool = True) -> None:
        """Assert s3 bucket."""
        try:
            self.s3.head_bucket(Bucket=bucket)
            if not present:
                self.fail(f"s3: bucket {bucket} should not exist")
        except ClientError as err:
            if err.response["Error"]["Code"] == "404" and present:
                self.fail(f"s3: bucket {bucket} does not exist")

    def setUp(self) -> None:
        """Run before tests."""
        self.context = CfnginContext(
            config=CfnginConfig.parse_obj(
                {"namespace": "test", "cfngin_bucket": "test"}
            )
        )
        self.provider = mock_provider(region="us-east-1")

    def run_hook(self, **kwargs: Any) -> Dict[Any, Any]:
        """Run hook."""
        real_kwargs = {
            "context": self.context,
            "provider": self.provider,
        }
        real_kwargs.update(kwargs)

        return upload_lambda_functions(**real_kwargs)  # type: ignore

    @mock_s3
    def test_bucket_default(self) -> None:
        """Test bucket default."""
        self.assertIsNotNone(self.run_hook(functions={}))

        self.assert_s3_bucket("test")

    @mock_s3
    def test_bucket_custom(self) -> None:
        """Test bucket custom."""
        self.assertIsNotNone(self.run_hook(bucket="custom", functions={}))

        self.assert_s3_bucket("test", present=False)
        self.assert_s3_bucket("custom")

    @mock_s3
    def test_prefix(self) -> None:
        """Test prefix."""
        with self.temp_directory_with_files() as temp_dir:
            results = self.run_hook(
                prefix="cloudformation-custom-resources/",
                functions={"MyFunction": {"path": temp_dir.path + "/f1"}},
            )

        self.assertIsNotNone(results)

        code = results.get("MyFunction")
        self.assertIsInstance(code, Code)
        self.assert_s3_zip_file_list(code.S3Bucket, code.S3Key, F1_FILES)
        self.assertTrue(
            code.S3Key.startswith("cloudformation-custom-resources/lambda-MyFunction-")
        )

    @mock_s3
    def test_prefix_missing(self) -> None:
        """Test prefix missing."""
        with self.temp_directory_with_files() as temp_dir:
            results = self.run_hook(
                functions={"MyFunction": {"path": temp_dir.path + "/f1"}}
            )

        self.assertIsNotNone(results)

        code = results.get("MyFunction")
        self.assertIsInstance(code, Code)
        self.assert_s3_zip_file_list(code.S3Bucket, code.S3Key, F1_FILES)
        self.assertTrue(code.S3Key.startswith("lambda-MyFunction-"))

    @mock_s3
    def test_path_missing(self) -> None:
        """Test path missing."""
        msg = "missing required property 'path' in function 'MyFunction'"
        with ShouldRaise(ValueError(msg)):
            self.run_hook(functions={"MyFunction": {}})

    @mock_s3
    def test_path_relative(self) -> None:
        """Test path relative."""
        with self.temp_directory_with_files(["test/test.py"]) as temp_dir:
            results = self.run_hook(
                functions={"MyFunction": {"path": "test"}},
                context=CfnginContext(
                    config=CfnginConfig.parse_obj(
                        {"namespace": "test", "cfngin_bucket": "test"}
                    ),
                    config_path=Path(str(temp_dir.path)),
                ),
            )

        self.assertIsNotNone(results)

        code = results.get("MyFunction")
        self.assertIsInstance(code, Code)
        self.assert_s3_zip_file_list(code.S3Bucket, code.S3Key, ["test.py"])

    @mock_s3
    def test_path_home_relative(self) -> None:
        """Test path home relative."""
        orig_expanduser = os.path.expanduser
        with self.temp_directory_with_files(["test.py"]) as temp_dir, patch(
            "os.path.expanduser"
        ) as mock1:
            test_path = "~/test"

            mock1.side_effect = lambda p: (  # type: ignore
                temp_dir.path if p == test_path else orig_expanduser(p)
            )

            results = self.run_hook(functions={"MyFunction": {"path": test_path}})

        self.assertIsNotNone(results)

        code = results.get("MyFunction")
        self.assertIsInstance(code, Code)
        self.assert_s3_zip_file_list(code.S3Bucket, code.S3Key, ["test.py"])

    @mock_s3
    def test_multiple_functions(self) -> None:
        """Test multiple functions."""
        with self.temp_directory_with_files() as temp_dir:
            results = self.run_hook(
                functions={
                    "MyFunction": {"path": temp_dir.path + "/f1"},
                    "OtherFunction": {"path": temp_dir.path + "/f2"},
                }
            )

        self.assertIsNotNone(results)

        f1_code = results.get("MyFunction")
        self.assertIsInstance(f1_code, Code)
        self.assert_s3_zip_file_list(f1_code.S3Bucket, f1_code.S3Key, F1_FILES)

        f2_code = results.get("OtherFunction")
        self.assertIsInstance(f2_code, Code)
        self.assert_s3_zip_file_list(f2_code.S3Bucket, f2_code.S3Key, F2_FILES)

    @mock_s3
    def test_patterns_invalid(self) -> None:
        """Test patterns invalid."""
        msg = (
            "Invalid file patterns in key 'include': must be a string or "
            "list of strings"
        )

        with ShouldRaise(ValueError(msg)):
            self.run_hook(
                functions={
                    "MyFunction": {"path": "test", "include": {"invalid": "invalid"}}
                }
            )

    @mock_s3
    def test_patterns_include(self) -> None:
        """Test patterns include."""
        with self.temp_directory_with_files() as temp_dir:
            results = self.run_hook(
                functions={
                    "MyFunction": {
                        "path": temp_dir.path + "/f1",
                        "include": ["*.py", "test2/"],
                    }
                }
            )

        self.assertIsNotNone(results)

        code = results.get("MyFunction")
        self.assertIsInstance(code, Code)
        self.assert_s3_zip_file_list(
            code.S3Bucket,
            code.S3Key,
            [
                "f1.py",
                "__init__.py",
                "test/__init__.py",
                "test/f1.py",
                "test2/test.txt",
            ],
        )

    @mock_s3
    def test_patterns_exclude(self) -> None:
        """Test patterns exclude."""
        with self.temp_directory_with_files() as temp_dir:
            results = self.run_hook(
                functions={
                    "MyFunction": {
                        "path": temp_dir.path + "/f1",
                        "exclude": ["*.pyc", "test/"],
                    }
                }
            )

        self.assertIsNotNone(results)

        code = results.get("MyFunction")
        self.assertIsInstance(code, Code)
        self.assert_s3_zip_file_list(
            code.S3Bucket, code.S3Key, ["f1.py", "__init__.py", "test2/test.txt"]
        )

    @mock_s3
    def test_patterns_include_exclude(self) -> None:
        """Test patterns include exclude."""
        with self.temp_directory_with_files() as temp_dir:
            results = self.run_hook(
                functions={
                    "MyFunction": {
                        "path": temp_dir.path + "/f1",
                        "include": "*.py",
                        "exclude": "test/",
                    }
                }
            )

        self.assertIsNotNone(results)

        code = results.get("MyFunction")
        self.assertIsInstance(code, Code)
        self.assert_s3_zip_file_list(
            code.S3Bucket, code.S3Key, ["f1.py", "__init__.py"]
        )

    @mock_s3
    def test_patterns_exclude_all(self) -> None:
        """Test patterns exclude all."""
        msg = (
            "Empty list of files for Lambda payload. Check your "
            "include/exclude options for errors."
        )

        with self.temp_directory_with_files() as temp_dir, ShouldRaise(
            RuntimeError(msg)
        ):

            results = self.run_hook(
                functions={
                    "MyFunction": {"path": temp_dir.path + "/f1", "exclude": ["**"]}
                }
            )

            self.assertIsNone(results)

    @mock_s3
    def test_idempotence(self) -> None:
        """Test idempotence."""
        with self.temp_directory_with_files() as temp_dir:
            functions = {"MyFunction": {"path": temp_dir.path + "/f1"}}

            bucket_name = "test"

            self.s3.create_bucket(Bucket=bucket_name)

            previous = None
            for _ in range(2):
                results = self.run_hook(bucket=bucket_name, functions=functions)
                self.assertIsNotNone(results)

                code = results.get("MyFunction")
                self.assertIsInstance(code, Code)

                if not previous:
                    previous = code.S3Key
                    continue

                compare(
                    previous,
                    code.S3Key,
                    prefix="zipfile name should not be modified in " "repeated runs.",
                )

    def test_calculate_hash(self) -> None:
        """Test calculate hash."""
        with self.temp_directory_with_files() as temp_dir1:
            root = cast(str, temp_dir1.path)
            hash1 = _calculate_hash(ALL_FILES, root)

        with self.temp_directory_with_files() as temp_dir2:
            root = cast(str, temp_dir2.path)
            hash2 = _calculate_hash(ALL_FILES, root)

        with self.temp_directory_with_files() as temp_dir3:
            root = cast(str, temp_dir3.path)
            with open(os.path.join(root, ALL_FILES[0]), "w") as _file:
                _file.write("modified file data")
            hash3 = _calculate_hash(ALL_FILES, root)

        self.assertEqual(hash1, hash2)
        self.assertNotEqual(hash1, hash3)
        self.assertNotEqual(hash2, hash3)

    def test_calculate_hash_diff_filename_same_contents(self) -> None:
        """Test calculate hash diff filename same contents."""
        files = ["file1.txt", "f2/file2.txt"]
        file1, file2 = files
        with TempDirectory() as temp_dir:
            root = cast(str, temp_dir.path)
            for file_name in files:
                temp_dir.write(file_name, b"data")
            hash1 = _calculate_hash([file1], root)
            hash2 = _calculate_hash([file2], root)
        self.assertNotEqual(hash1, hash2)

    def test_calculate_hash_different_ordering(self) -> None:
        """Test calculate hash different ordering."""
        files1 = ALL_FILES
        files2 = random.sample(ALL_FILES, k=len(ALL_FILES))
        with TempDirectory() as temp_dir1:
            root1 = cast(str, temp_dir1.path)
            for file_name in files1:
                temp_dir1.write(file_name, b"")
            with TempDirectory() as temp_dir2:
                root2 = cast(str, temp_dir2.path)
                for file_name in files2:
                    temp_dir2.write(file_name, b"")
                hash1 = _calculate_hash(files1, root1)
                hash2 = _calculate_hash(files2, root2)
                self.assertEqual(hash1, hash2)

    def test_select_bucket_region(self) -> None:
        """Test select bucket region."""
        tests: Tuple[
            Tuple[Tuple[Optional[str], Optional[str], Optional[str], str], str], ...
        ] = (
            (("myBucket", "us-east-1", "us-west-1", "eu-west-1"), "us-east-1"),
            (("myBucket", None, "us-west-1", "eu-west-1"), "eu-west-1"),
            ((None, "us-east-1", "us-west-1", "eu-west-1"), "us-west-1"),
            ((None, "us-east-1", None, "eu-west-1"), "eu-west-1"),
        )

        for args, result in tests:
            self.assertEqual(select_bucket_region(*args), result)  # type: ignore

    @mock_s3
    def test_follow_symlink_nonbool(self) -> None:
        """Test follow symlink nonbool."""
        msg = "follow_symlinks option must be a boolean"
        with ShouldRaise(ValueError(msg)):
            self.run_hook(
                follow_symlinks="raiseValueError", functions={"MyFunction": {}}
            )

    @mock_s3
    def test_follow_symlink_true(self) -> None:
        """Testing if symlinks are followed."""
        with self.temp_directory_with_files() as temp_dir1:
            root1 = temp_dir1.path
            with self.temp_directory_with_files() as temp_dir2:
                root2 = temp_dir2.path
                os.symlink(root1 + "/f1", root2 + "/f3")
                results = self.run_hook(
                    follow_symlinks=True, functions={"MyFunction": {"path": root2}}
                )
            self.assertIsNotNone(results)

            code = results.get("MyFunction")
            self.assertIsInstance(code, Code)
            self.assert_s3_zip_file_list(
                code.S3Bucket,
                code.S3Key,
                [
                    "f1/f1.py",
                    "f1/__init__.py",
                    "f1/f1.pyc",
                    "f1/test/__init__.py",
                    "f1/test/f1.py",
                    "f1/test/f1.pyc",
                    "f1/test2/test.txt",
                    "f2/f2.js",
                    "f3/__init__.py",
                    "f3/f1.py",
                    "f3/f1.pyc",
                    "f3/test/__init__.py",
                    "f3/test/f1.py",
                    "f3/test/f1.pyc",
                    "f3/test2/test.txt",
                ],
            )

    @mock_s3
    def test_follow_symlink_false(self) -> None:
        """Testing if syminks are present and not followed."""
        with self.temp_directory_with_files() as temp_dir1:
            root1 = temp_dir1.path
            with self.temp_directory_with_files() as temp_dir2:
                root2 = temp_dir2.path
                os.symlink(root1 + "/f1", root2 + "/f3")
                results = self.run_hook(
                    follow_symlinks=False, functions={"MyFunction": {"path": root2}}
                )
            self.assertIsNotNone(results)

            code = results.get("MyFunction")
            self.assertIsInstance(code, Code)
            self.assert_s3_zip_file_list(
                code.S3Bucket,
                code.S3Key,
                [
                    "f1/f1.py",
                    "f1/__init__.py",
                    "f1/f1.pyc",
                    "f1/test/__init__.py",
                    "f1/test/f1.py",
                    "f1/test/f1.pyc",
                    "f1/test2/test.txt",
                    "f2/f2.js",
                ],
            )

    @mock_s3
    def test_follow_symlink_omitted(self) -> None:
        """Same as test_follow_symlink_false, but default behavior."""
        with self.temp_directory_with_files() as temp_dir1:
            root1 = temp_dir1.path
            with self.temp_directory_with_files() as temp_dir2:
                root2 = temp_dir2.path
                os.symlink(root1 + "/f1", root2 + "/f3")
                results = self.run_hook(functions={"MyFunction": {"path": root2}})
            self.assertIsNotNone(results)

            code = results.get("MyFunction")
            self.assertIsInstance(code, Code)
            self.assert_s3_zip_file_list(
                code.S3Bucket,
                code.S3Key,
                [
                    "f1/f1.py",
                    "f1/__init__.py",
                    "f1/f1.pyc",
                    "f1/test/__init__.py",
                    "f1/test/f1.py",
                    "f1/test/f1.pyc",
                    "f1/test2/test.txt",
                    "f2/f2.js",
                ],
            )

    @mock_s3
    @patch("runway.cfngin.hooks.aws_lambda.subprocess")
    @patch(
        "runway.cfngin.hooks.aws_lambda.find_requirements",
        MagicMock(
            return_value={
                "requirements.txt": True,
                "Pipfile": False,
                "Pipfile.lock": False,
            }
        ),
    )
    @patch("runway.cfngin.hooks.aws_lambda.copydir", MagicMock())
    @patch(
        "runway.cfngin.hooks.aws_lambda.handle_requirements",
        MagicMock(return_value="./tests/requirements.txt"),
    )
    @patch("runway.cfngin.hooks.aws_lambda._find_files", MagicMock())
    @patch(
        "runway.cfngin.hooks.aws_lambda._zip_files",
        MagicMock(return_value=("zip_contents", "content_hash")),
    )
    @patch("runway.cfngin.hooks.aws_lambda._upload_code", MagicMock())
    @patch("runway.cfngin.hooks.aws_lambda.sys")
    def test_frozen(self, mock_sys: MagicMock, mock_proc: MagicMock) -> None:
        """Test building with pip when frozen."""
        mock_sys.frozen = True
        mock_sys.version_info = sys.version_info
        with self.temp_directory_with_files() as temp_dir:
            self.run_hook(
                functions={
                    "MyFunction": {
                        "path": temp_dir.path + "/f1",
                        "include": ["*.py", "test2/"],
                    }
                }
            )
        mock_proc.check_call.assert_called_once_with([ANY, "run-python", ANY])
        assert mock_proc.check_call.call_args.args[0][2].endswith(
            "__runway_run_pip_install.py"
        )


class TestDockerizePip:
    """Test dockerize_pip."""

    command = [
        "/bin/sh",
        "-c",
        "python -m pip install -t /var/task -r /var/task/requirements.txt",
    ]
    host_config = {
        "NetworkMode": "default",
        "AutoRemove": True,
        "Mounts": [
            {
                "Target": "/var/task",
                "Source": os.getcwd(),
                "Type": "bind",
                "ReadOnly": False,
            }
        ],
    }

    def test_with_docker_file(self) -> None:
        """Test with docker_file provided."""
        client = make_fake_client()
        with TempDirectory() as tmp_dir:
            docker_file = tmp_dir.write("Dockerfile", b"")
            dockerized_pip(os.getcwd(), client=client, docker_file=docker_file)

            # for some reason, the current version of pylint does not see these methods
            # pylint: disable=no-member
            client.api.build.assert_called_with(
                path=tmp_dir.path, dockerfile="Dockerfile", forcerm=True
            )
            client.api.create_container.assert_called_with(
                detach=True,
                image=FAKE_IMAGE_ID,
                command=self.command,
                host_config=self.host_config,
            )
            client.api.inspect_container.assert_called_with(FAKE_CONTAINER_ID)
            client.api.start.assert_called_with(FAKE_CONTAINER_ID)
            client.api.logs.assert_called_with(
                FAKE_CONTAINER_ID, stderr=True, stdout=True, stream=True, tail=0
            )

    def test_with_docker_image(self) -> None:
        """Test with docker_image provided."""
        client = make_fake_client()
        image = "alpine"
        dockerized_pip(os.getcwd(), client=client, docker_image=image)

        # for some reason, the current version of pylint does not see these methods
        # pylint: disable=no-member
        client.api.create_container.assert_called_with(
            detach=True, image=image, command=self.command, host_config=self.host_config
        )
        client.api.inspect_container.assert_called_with(FAKE_CONTAINER_ID)
        client.api.start.assert_called_with(FAKE_CONTAINER_ID)
        client.api.logs.assert_called_with(
            FAKE_CONTAINER_ID, stderr=True, stdout=True, stream=True, tail=0
        )

    def test_with_runtime(self) -> None:
        """Test with runtime provided."""
        client = make_fake_client()
        runtime = "python3.8"
        dockerized_pip(os.getcwd(), client=client, runtime=runtime)

        # for some reason, the current version of pylint does not see these methods
        # pylint: disable=no-member
        client.api.create_container.assert_called_with(
            detach=True,
            image="lambci/lambda:build-" + runtime,
            command=self.command,
            host_config=self.host_config,
        )
        client.api.inspect_container.assert_called_with(FAKE_CONTAINER_ID)
        client.api.start.assert_called_with(FAKE_CONTAINER_ID)
        client.api.logs.assert_called_with(
            FAKE_CONTAINER_ID, stderr=True, stdout=True, stream=True, tail=0
        )

    def test_raises_invalid_config(self) -> None:
        """Test that InvalidDockerizePipConfiguration is raised."""
        client = make_fake_client()
        with pytest.raises(InvalidDockerizePipConfiguration):
            dockerized_pip(
                os.getcwd(),
                client=client,
                docker_file="docker_file",
                docker_image="docker_image",
                runtime="runtime",
            )
        with pytest.raises(InvalidDockerizePipConfiguration):
            dockerized_pip(
                os.getcwd(),
                client=client,
                docker_file="docker_file",
                docker_image="docker_image",
            )
        with pytest.raises(InvalidDockerizePipConfiguration):
            dockerized_pip(
                os.getcwd(), client=client, docker_file="docker_file", runtime="runtime"
            )
        with pytest.raises(InvalidDockerizePipConfiguration):
            dockerized_pip(
                os.getcwd(),
                client=client,
                docker_image="docker_image",
                runtime="runtime",
            )
        with pytest.raises(InvalidDockerizePipConfiguration):
            dockerized_pip(os.getcwd(), client=client)

    def test_raises_value_error_missing_dockerfile(self) -> None:
        """ValueError raised when provided Dockerfile is not found."""
        client = make_fake_client()
        with pytest.raises(ValueError) as excinfo:
            dockerized_pip(os.getcwd(), client=client, docker_file="not-a-Dockerfile")
        assert "docker_file" in str(excinfo.value)

    def test_raises_value_error_runtime(self) -> None:
        """ValueError raised if runtime provided is not supported."""
        client = make_fake_client()
        with pytest.raises(ValueError) as excinfo:
            dockerized_pip(os.getcwd(), client=client, runtime="node")
        assert "node" in str(excinfo.value)


class TestHandleRequirements:
    """Test handle_requirements."""

    PIPFILE = "\n".join(
        [
            "[[source]]",
            'url = "https://pypi.org/simple"',
            "verify_ssl = true",
            'name = "pypi"',
            "[packages]",
            "[dev-packages]",
        ]
    )
    REQUIREMENTS = "-i https://pypi.org/simple\n\n"

    def test_default(self) -> None:
        """Test default action."""
        with TempDirectory() as tmp_dir:
            tmp_dir.write("Pipfile", self.PIPFILE.encode("utf-8"))
            expected = b"This is correct."
            tmp_dir.write("requirements.txt", expected)
            req_path = handle_requirements(
                package_root=cast(str, tmp_dir.path),
                dest_path=cast(str, tmp_dir.path),
                requirements=cast(
                    Dict[str, bool], find_requirements(cast(str, tmp_dir.path))
                ),
            )

            assert req_path == os.path.join(cast(str, tmp_dir.path), "requirements.txt")
            assert not os.path.isfile(
                os.path.join(cast(str, tmp_dir.path), "Pipfile.lock")
            )
            assert tmp_dir.read("requirements.txt") == expected

    def test_explicit_pipenv(self) -> None:
        """Test with 'use_pipenv=True'."""
        with TempDirectory() as tmp_dir:
            tmp_dir.write("Pipfile", self.PIPFILE.encode("utf-8"))
            tmp_dir.write("requirements.txt", b"This is not correct!")
            req_path = handle_requirements(
                package_root=cast(str, tmp_dir.path),
                dest_path=cast(str, tmp_dir.path),
                requirements=cast(
                    Dict[str, bool], find_requirements(cast(str, tmp_dir.path))
                ),
                use_pipenv=True,
            )

            assert req_path == os.path.join(cast(str, tmp_dir.path), "requirements.txt")
            assert tmp_dir.read("Pipfile.lock")
            req_txt = tmp_dir.read("requirements.txt")
            if isinstance(req_txt, bytes):
                req_txt = req_txt.decode()
            if req_txt.startswith(
                "#\n# These requirements were autogenerated by pipenv\n"
            ):
                assert req_txt == (
                    "#\n# These requirements were autogenerated by pipenv\n"
                    "# To regenerate from the project's Pipfile, run:\n#\n"
                    "#    pipenv lock --requirements\n#\n\n"
                    "-i https://pypi.org/simple\n\n"
                )
            else:  # older version of pipenv is being used
                assert req_txt == "-i https://pypi.org/simple\n\n"

    def test_frozen_pipenv(
        self, caplog: LogCaptureFixture, monkeypatch: MonkeyPatch, tmp_path: Path
    ) -> None:
        """Test use pipenv from Pyinstaller build."""
        caplog.set_level(logging.ERROR, logger="runway.cfngin.hooks.aws_lambda")
        monkeypatch.setattr(
            "runway.cfngin.hooks.aws_lambda.sys.frozen", True, raising=False
        )

        with pytest.raises(SystemExit) as excinfo:
            handle_requirements(
                package_root=str(tmp_path),
                dest_path=str(tmp_path),
                requirements={
                    "requirements.txt": False,
                    "Pipfile": True,
                    "Pipfile.lock": False,
                },
            )
        assert excinfo.value.code == 1
        assert [
            "pipenv can only be used with python installed from PyPi"
        ] == caplog.messages

    def test_implicit_pipenv(self) -> None:
        """Test implicit use of pipenv."""
        with TempDirectory() as tmp_dir:
            tmp_dir.write("Pipfile", self.PIPFILE.encode("utf-8"))
            req_path = handle_requirements(
                package_root=cast(str, tmp_dir.path),
                dest_path=cast(str, tmp_dir.path),
                requirements=cast(
                    Dict[str, bool], find_requirements(cast(str, tmp_dir.path))
                ),
            )

            assert req_path == os.path.join(cast(str, tmp_dir.path), "requirements.txt")
            assert tmp_dir.read("Pipfile.lock")
            req_txt = tmp_dir.read("requirements.txt")
            if isinstance(req_txt, bytes):
                req_txt = req_txt.decode()
            if req_txt.startswith(
                "#\n# These requirements were autogenerated by pipenv\n"
            ):
                assert req_txt == (
                    "#\n# These requirements were autogenerated by pipenv\n"
                    "# To regenerate from the project's Pipfile, run:\n#\n"
                    "#    pipenv lock --requirements\n#\n\n"
                    "-i https://pypi.org/simple\n\n"
                )
            else:  # older version of pipenv is being used
                assert req_txt == "-i https://pypi.org/simple\n\n"

    def test_raise_not_implimented(self) -> None:
        """Test NotImplimentedError is raised when no requirements file."""
        with TempDirectory() as tmp_dir:
            with pytest.raises(NotImplementedError):
                handle_requirements(
                    package_root=cast(str, tmp_dir.path),
                    dest_path=cast(str, tmp_dir.path),
                    requirements={
                        "requirements.txt": False,
                        "Pipfile": False,
                        "Pipfile.lock": False,
                    },
                )


class TestShouldUseDocker:
    """Test should_use_docker."""

    def test_bool_true(self) -> None:
        """Test value bool(True)."""
        assert should_use_docker(True)

    def test_bool_false(self) -> None:
        """Test value bool(True)."""
        assert not should_use_docker(False)

    def test_str_true(self) -> None:
        """Test value 'false'."""
        assert should_use_docker("True")
        assert should_use_docker("true")

    def test_str_false(self) -> None:
        """Test value 'false'."""
        assert not should_use_docker("False")
        assert not should_use_docker("false")

    def test_non_linux(self) -> None:
        """Test value 'non-linux' with all possible platforms."""
        non_linux_os = ["aix", "cygwin", "darwin", "win32"]
        for non_linux in non_linux_os:
            with patch("runway.cfngin.hooks.aws_lambda.sys") as mock_sys:
                mock_sys.configure_mock(platform=non_linux)
                assert should_use_docker("non-linux")
        with patch("runway.cfngin.hooks.aws_lambda.sys") as mock_sys:
            mock_sys.configure_mock(platform="linux")
            assert not should_use_docker("non-linux")


def test_copydir() -> None:
    """Test copydir."""
    with TempDirectory() as tmp_dir:
        dest_path = tmp_dir.makedir("dest")
        src_path = tmp_dir.makedir("src")
        tmp_dir.makedir(("src", "lib"))
        example_file = b"example file content"
        tmp_dir.write(("src", "example_file"), example_file)
        tmp_dir.write(("src", "lib", "example_file"), example_file)

        copydir(src_path, dest_path, ["**"])

        assert tmp_dir.read(("src", "example_file")) == example_file
        assert tmp_dir.read(("src", "lib", "example_file")) == example_file
        assert tmp_dir.read(("dest", "example_file")) == example_file
        assert tmp_dir.read(("dest", "lib", "example_file")) == example_file

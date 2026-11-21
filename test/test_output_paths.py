"""Repository-local output and dependency-cache checks."""

from contextlib import chdir, redirect_stderr
from io import StringIO
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from momentum import main
from ouput.paths import PROJECT_ROOT, configure_plotting, output_path


class OutputPathsTest(unittest.TestCase):
    def test_relative_paths_ignore_working_directory(self):
        with TemporaryDirectory(dir=PROJECT_ROOT) as directory, chdir(directory):
            self.assertEqual(output_path("ouput/prices.csv"), PROJECT_ROOT / "ouput/prices.csv")

    def test_outside_absolute_parent_and_symlink_paths_are_rejected(self):
        with TemporaryDirectory(dir=PROJECT_ROOT) as directory:
            root = Path(directory)
            link = root / "outside"
            link.symlink_to(PROJECT_ROOT.parent, target_is_directory=True)
            with patch("ouput.paths.PROJECT_ROOT", root):
                for value in (PROJECT_ROOT.parent / "prices.csv", "../prices.csv", "outside/prices.csv"):
                    with self.subTest(value=value), self.assertRaises(ValueError):
                        output_path(value)

    def test_cli_rejects_external_outputs_before_loading_credentials(self):
        for option in ("--output", "--save-prices", "--cache-path"):
            with (
                self.subTest(option=option),
                patch("momentum._credentials_client") as credentials,
                redirect_stderr(StringIO()) as stderr,
                self.assertRaises(SystemExit) as error,
            ):
                main(["--tradingview", "--as-of", "2020-01-10",
                      option, str(PROJECT_ROOT.parent / "external.csv")])
            self.assertEqual(error.exception.code, 2)
            self.assertIn("inside the repository", stderr.getvalue())
            credentials.assert_not_called()

    def test_plotting_overrides_external_cache_environment(self):
        with patch.dict(os.environ, {"MPLCONFIGDIR": "/outside/matplotlib"}):
            configure_plotting()
            self.assertEqual(
                Path(os.environ["MPLCONFIGDIR"]), PROJECT_ROOT / ".cache/matplotlib",
            )

    def test_cold_home_imports_and_plotting_do_not_write_outside_repository(self):
        with TemporaryDirectory(dir=PROJECT_ROOT) as directory:
            environment = dict(
                os.environ, HOME=directory,
                XDG_CACHE_HOME=str(Path(directory) / "cache"),
                XDG_CONFIG_HOME=str(Path(directory) / "config"),
                PYTHONPATH=str(PROJECT_ROOT), MPLBACKEND="Agg",
            )
            environment.pop("MPLCONFIGDIR", None)
            code = """
import os
from pathlib import Path
import sys
root = Path(sys.argv[1])
home = Path.home()
def audit(event, args):
    path = None
    if event == "open" and isinstance(args[0], str) and isinstance(args[2], int):
        if args[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT):
            path = Path(args[0]).resolve()
    elif event == "os.mkdir":
        path = Path(args[0]).resolve()
    if path is not None and (not path.is_relative_to(root) or path.is_relative_to(home)):
        raise RuntimeError(f"External write blocked: {path}")
sys.addaudithook(audit)
import core_asset_risk_parity
assert "matplotlib.pyplot" not in sys.modules
from ouput.paths import configure_plotting
configure_plotting()
import riskfolio
import matplotlib
import matplotlib.pyplot as plt
assert Path(matplotlib.get_cachedir()) == root / ".cache/matplotlib"
assert Path(matplotlib.get_configdir()) == root / ".cache/matplotlib"
figure = plt.figure()
figure.canvas.draw()
plt.close(figure)
"""
            subprocess.run(
                [sys.executable, "-c", code, str(PROJECT_ROOT)],
                cwd=directory, env=environment, check=True, capture_output=True, text=True,
            )

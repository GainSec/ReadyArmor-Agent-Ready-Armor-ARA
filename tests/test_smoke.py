from ara import __version__
from ara.cli import main


def test_version_constant():
    assert isinstance(__version__, str) and __version__


def test_cli_no_args_prints_help(capsys):
    assert main([]) == 0
    out = capsys.readouterr().out
    assert "ara" in out
    assert "launch" in out


def test_cli_launch_without_cmd_errors(capsys):
    assert main(["launch"]) == 2
    err = capsys.readouterr().err
    assert "need a command" in err

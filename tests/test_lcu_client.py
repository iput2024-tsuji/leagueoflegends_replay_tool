from src.lcu_client import LCUConnectionInfo, LCUConnectionProvider, parse_lcu_command_line, parse_lcu_lockfile


def test_parse_lcu_command_line_reads_port_and_token():
    command_line = (
        '"C:\\Riot Games\\League of Legends\\LeagueClientUx.exe" '
        '--app-port=54321 --remoting-auth-token="secret-token"'
    )

    info = parse_lcu_command_line(command_line)

    assert info == LCUConnectionInfo(port=54321, password="secret-token")
    assert info.base_url == "https://127.0.0.1:54321"


def test_parse_lcu_lockfile_reads_connection_info():
    info = parse_lcu_lockfile("LeagueClient:1234:61234:password:https")

    assert info == LCUConnectionInfo(port=61234, password="password", protocol="https")


def test_lcu_connection_provider_uses_lockfile_override(tmp_path):
    lockfile = tmp_path / "lockfile"
    lockfile.write_text("LeagueClient:1234:61234:password:https", encoding="utf-8")
    provider = LCUConnectionProvider(
        command_line_reader=lambda: None,
        environ={"LOL_REPLAY_TOOL_LCU_LOCKFILE": str(lockfile)},
        retry_interval_sec=0,
    )

    assert provider.get_connection_info() == LCUConnectionInfo(
        port=61234,
        password="password",
        protocol="https",
    )


def test_lcu_connection_provider_can_invalidate_cached_credentials():
    command_lines = iter(
        [
            "--app-port=50001 --remoting-auth-token=first",
            "--app-port=50002 --remoting-auth-token=second",
        ]
    )
    provider = LCUConnectionProvider(
        command_line_reader=lambda: next(command_lines),
        environ={},
        retry_interval_sec=0,
    )

    assert provider.get_connection_info().port == 50001
    provider.invalidate()
    assert provider.get_connection_info().port == 50002

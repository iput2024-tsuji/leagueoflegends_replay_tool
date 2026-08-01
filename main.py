from __future__ import annotations

import sys


def _configure_self_check_streams() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="backslashreplace")


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--self-check" in args:
        _configure_self_check_streams()
        from src.self_check import format_self_check_report, report_as_json, run_self_check, self_check_exit_code

        report = run_self_check()
        if "--json" in args:
            print(report_as_json(report))
        else:
            print(format_self_check_report(report))
        return self_check_exit_code(report)

    from src.app import main as app_main

    app_main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

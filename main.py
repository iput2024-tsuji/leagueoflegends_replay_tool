from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--self-check" in args:
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

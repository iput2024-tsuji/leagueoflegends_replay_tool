from types import SimpleNamespace

from src.app import HomePage, MainWindow


def test_home_page_only_displays_recorder_status_badge(qtbot):
    page = HomePage(lambda: None, lambda: None, lambda: None)
    qtbot.addWidget(page)

    page.set_recorder_status("🔴 録画中", color_hex="#ff6b6b")

    assert page.status_label.text() == "🔴 録画中"
    assert not hasattr(page, "status_detail_label")


def test_worker_message_updates_badge_without_detail_text():
    calls = []
    home_page = SimpleNamespace(set_recorder_status=lambda *args, **kwargs: calls.append((args, kwargs)))
    window = SimpleNamespace(home_page=home_page)
    window._derive_recorder_home_status = lambda message: MainWindow._derive_recorder_home_status(window, message)

    MainWindow._set_home_status_from_worker_message(window, "🛡️  試合終了を監視中...")

    assert calls == [(("🔴 録画中",), {"color_hex": "#ff6b6b"})]

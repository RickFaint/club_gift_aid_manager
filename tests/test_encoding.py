"""Guard browser-facing source files against common UTF-8 mojibake."""

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
MOJIBAKE_MARKERS = ("ï", "â", "Â", "Ã")


def test_browser_sources_do_not_contain_mojibake() -> None:
    """Rendered HTML, JavaScript, and CSS must not contain broken UTF-8 text."""
    files = [
        *PROJECT_ROOT.joinpath("templates").rglob("*.html"),
        *PROJECT_ROOT.joinpath("static").rglob("*.js"),
        *PROJECT_ROOT.joinpath("static").rglob("*.css"),
    ]
    failures = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        if any(marker in text for marker in MOJIBAKE_MARKERS):
            failures.append(str(path.relative_to(PROJECT_ROOT)))

    assert failures == [], "Mojibake found in: " + ", ".join(failures)

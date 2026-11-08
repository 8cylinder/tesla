from importlib.metadata import version as pkg_version

project = "tesla"
author = "Sheldon McGrandle"
copyright = "2026, Sheldon McGrandle"
release = pkg_version("tesla")

extensions = ["myst_parser", "sphinx_click"]
source_suffix = {".rst": "restructuredtext", ".md": "markdown"}
root_doc = "index"
exclude_patterns = ["_build"]
html_theme = "furo"
myst_heading_anchors = 3

man_pages = [
    (
        "cli",
        "tesla",
        "Tesla Fleet API CLI: Powerwall-aware vehicle charging",
        ["Sheldon McGrandle"],
        1,
    ),
]

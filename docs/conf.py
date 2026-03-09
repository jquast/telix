# std imports
import os
import sys
import datetime

sys.path.insert(
    0, os.path.abspath(os.path.join(os.path.dirname(__file__), os.path.pardir))
)

suppress_warnings = ["image.nonlocal_uri", "myst.header"]

# -- General configuration ----------------------------------------------------

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.intersphinx",
    "sphinx.ext.viewcode",
    "myst_parser",
]

templates_path = ["_templates"]

source_suffix = {
    ".rst": "restructuredtext",
    ".md": "markdown",
}

master_doc = "index"

project = "Telix"
copyright = f"2025-{datetime.datetime.now().year} Jeff Quast"

version = "0"
release = "0.0.1"  # keep in sync with pyproject.toml

exclude_patterns = ["_build"]

add_function_parentheses = True
add_module_names = False
pygments_style = "sphinx"

# -- Options for HTML output --------------------------------------------------

html_theme = "alabaster"
html_theme_options = {
    "description": "A modern telnet client for BBSs and MUDs",
    "github_user": "jquast",
    "github_repo": "telix",
    "github_type": "star",
    "fixed_sidebar": True,
}

html_static_path = ["_static"]
html_css_files = ["custom.css"]

html_split_index = True
html_show_sourcelink = True
html_show_sphinx = False
html_show_copyright = True
htmlhelp_basename = "telix_doc"

# -- Options for LaTeX output -------------------------------------------------

latex_documents = [
    ("index", "telix.tex", "Telix Documentation", "Jeff Quast", "manual"),
]

# -- Options for manual page output -------------------------------------------

man_pages = [("index", "telix", "Telix Documentation", ["Jeff Quast"], 1)]

autodoc_member_order = "bysource"

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
}

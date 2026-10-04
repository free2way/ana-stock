"""Shared CSS design tokens.

Single source of truth for the ``:root`` custom-property blocks repeated
across page styles (dashboard, screener, and the smaller route pages).
Each constant holds the complete ``:root { ... }`` selector block, byte-exact
as it appears in the rendered pages, so style constants compose them via
string concatenation without altering rendered output.

Variants:
- ``ROOT_TOKENS_BASE``          -- core palette (bg/panel/ink/muted/line/accent)
- ``ROOT_TOKENS_BASE_PANEL2``   -- base + ``--panel-2``
- ``ROOT_TOKENS_BASE_SOFT``     -- base + ``--panel-2`` + ``--accent-soft``
- ``ROOT_TOKENS_BASE_SIGNALS``  -- base + ``--warn``/``--danger``/``--blue``
- ``ROOT_TOKENS_SOFT_MULTILINE``-- same tokens as BASE_SOFT, spaced layout
- ``ROOT_TOKENS_SURFACE``       -- ops pages surface palette
- ``ROOT_TOKENS_LAB``           -- screener lab pages palette
"""

ROOT_TOKENS_BASE = ":root { --bg:#071018; --panel:#111c28; --ink:#e6edf3; --muted:#90a3b8; --line:#223246; --accent:#3dd9b6; }"

ROOT_TOKENS_BASE_PANEL2 = ":root { --bg:#071018; --panel:#111c28; --panel-2:#152231; --ink:#e6edf3; --muted:#90a3b8; --line:#223246; --accent:#3dd9b6; }"

ROOT_TOKENS_BASE_SOFT = ":root { --bg:#071018; --panel:#111c28; --panel-2:#152231; --ink:#e6edf3; --muted:#90a3b8; --line:#223246; --accent:#3dd9b6; --accent-soft:rgba(61,217,182,0.12); }"

ROOT_TOKENS_BASE_SIGNALS = ":root { --bg:#071018; --panel:#111c28; --ink:#e6edf3; --muted:#90a3b8; --line:#223246; --accent:#3dd9b6; --warn:#fbbf24; --danger:#fb7185; --blue:#60a5fa; }"

ROOT_TOKENS_SOFT_MULTILINE = (
    ":root {\n"
    "            --bg: #071018;\n"
    "            --panel: #111c28;\n"
    "            --panel-2: #152231;\n"
    "            --ink: #e6edf3;\n"
    "            --muted: #90a3b8;\n"
    "            --line: #223246;\n"
    "            --accent: #3dd9b6;\n"
    "            --accent-soft: rgba(61,217,182,0.12);\n"
    "          }"
)

ROOT_TOKENS_SURFACE = ":root{--bg:#071018;--surface:#0e1926;--ink:#e8eef6;--muted:#91a4b8;--line:#23374a;--accent:#43d7b8}"

ROOT_TOKENS_LAB = ":root { --bg:#07111b; --panel:#0d1824; --panel2:#111f2d; --line:#213447; --ink:#edf5f2; --muted:#8da1ad; --accent:#35e0b4; --warn:#f8c95d; --bad:#fb7185; }"

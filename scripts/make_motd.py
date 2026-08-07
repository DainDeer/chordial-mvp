"""generates the ssh login banner in assets/: the wordmark "chorial" with an
eighth note standing in for the d, shaded left-to-right in the nonbinary pride
colors, framed by a rainbow pride stripe above and a trans pride stripe below.

    poetry run python scripts/make_motd.py             > assets/motd_plain.txt
    poetry run python scripts/make_motd.py --color      > assets/motd.txt

assets/motd.txt (256-color ANSI) is what actually gets installed as the
server's /etc/motd; the plain file is a colorless fallback kept alongside it.
"""
import sys

letters = {
    "c": ["      ", "  ___ ", " / __|", "| (__ ", " \\___|"],
    "h": [" _     ", "| |__  ", "| '_ \\ ", "| | | |", "|_| |_|"],
    "o": ["       ", "  ___  ", " / _ \\ ", "| (_) |", " \\___/ "],
    "r": ["       ", " _ __  ", "| '__| ", "| |    ", "|_|    "],
    "i": [" _ ", "(_)", "| |", "| |", "|_|"],
    "a": ["       ", "  __ _ ", " / _` |", "| (_| |", " \\__,_|"],
    "l": [" _  ", "| | ", "| | ", "| | ", "|_| "],
}
NOTE = [
    "   | |\\   ",
    "   | | )  ",
    "   | |/   ",
    "   | |    ",
    " __| |    ",
    "(___/     ",
]
for k in letters:
    letters[k] = [" " * len(letters[k][0])] + letters[k]  # blank row above, for the note's flag
letters["D"] = NOTE  # "D" is the note slot in the word below, not a literal letter d

WORD = "chorDial"
# negative tracking: how many columns a letter slides left into the previous
# letter's box (glyphs merge where one has whitespace). D=4 tucks the notehead
# under the r's overhang; i=2 pulls the i in under the note's flag curl.
KERN = {"D": 3, "i": 2}


def compose():
    x, spans = 0, []
    for ch in WORD:
        start = max(0, x - KERN.get(ch, 0))
        spans.append((ch, start))
        x = start + len(letters[ch][0])
    canvas = [[" "] * x for _ in range(6)]
    for ch, start in spans:
        for r, row in enumerate(letters[ch]):
            for j, c in enumerate(row):
                if c != " ":
                    if canvas[r][start + j] != " ":
                        raise ValueError(f"glyph collision at row {r}, col {start + j}")
                    canvas[r][start + j] = c
    return ["".join(row).rstrip() for row in canvas]


ROWS = compose()
WIDTH = max(len(r) for r in ROWS)

RAINBOW = [196, 208, 226, 46, 33, 129]         # rainbow pride flag, 6 stripes
# nonbinary pride flag: yellow, white, purple, black. the black stripe is a dark
# grey here so the glyphs still read against a black terminal background.
NONBINARY = [226, 15, 141, 240]
TRANS = [117, 218, 15, 218, 117]               # trans pride flag, 5 stripes


def stripe_line(palette):
    n = len(palette)
    out = []
    for c in range(WIDTH):
        color = palette[min(c * n // WIDTH, n - 1)]
        out.append(f"\033[38;5;{color}m-")
    return "".join(out) + "\033[0m"


def art_colored(palette):
    n = len(palette)
    reach = WIDTH + (len(ROWS) - 1) * 2       # largest c + r * 2 the sweep sees
    out = []
    for r, row in enumerate(ROWS):
        line = []
        for c, ch in enumerate(row):
            if ch == " ":
                line.append(ch)
            else:
                color = palette[min((c + r * 2) * n // reach, n - 1)]  # diagonal sweep
                line.append(f"\033[38;5;{color}m{ch}")
        out.append("".join(line) + "\033[0m")
    return out


if __name__ == "__main__":
    if "--color" in sys.argv:
        print(stripe_line(RAINBOW))
        print("\n".join(art_colored(NONBINARY)))
        print(stripe_line(TRANS))
    else:
        print("-" * WIDTH)
        print("\n".join(ROWS))
        print("-" * WIDTH)

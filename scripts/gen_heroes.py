"""Generate original tiny-RPG pixel-art characters (copyright-clean).

Each hero is drawn on a 16x18 transparent canvas with flat colors, then a
1px dark outline is added around the whole silhouette for that classic
pixel-sprite read. Saved at native resolution; the UI scales them up with
image-rendering:pixelated so edges stay crisp.
"""
from PIL import Image

W, H = 16, 18
OUT = (28, 24, 38, 255)     # near-black outline
EYE = (20, 18, 28, 255)

def C(*rgb):  # opaque color helper
    return (rgb[0], rgb[1], rgb[2], 255)

# palettes
SKIN  = C(240, 198, 160)
SKIN2 = C(120, 180, 110)   # goblin/orc green
BONE  = C(235, 235, 225)

def canvas():
    return Image.new("RGBA", (W, H), (0, 0, 0, 0))

def rect(px, x0, y0, x1, y1, col):
    for y in range(y0, y1 + 1):
        for x in range(x0, x1 + 1):
            if 0 <= x < W and 0 <= y < H:
                px[x, y] = col

def dot(px, x, y, col):
    if 0 <= x < W and 0 <= y < H:
        px[x, y] = col

def outline(im):
    """Add a 1px OUTLINE around every opaque region (silhouette border)."""
    px = im.load()
    out = im.copy()
    opx = out.load()
    for y in range(H):
        for x in range(W):
            if px[x, y][3] == 0:  # transparent — outline if touching opaque
                for dx, dy in ((1,0),(-1,0),(0,1),(0,-1)):
                    nx, ny = x+dx, y+dy
                    if 0 <= nx < W and 0 <= ny < H and px[nx, ny][3] != 0:
                        opx[x, y] = OUT
                        break
    return out

# ---- body parts ----
def legs(px, col, x=5):
    rect(px, x, 15, x+1, 17, col)
    rect(px, x+4, 15, x+5, 17, col)

def torso(px, col, trim=None):
    rect(px, 4, 10, 11, 14, col)     # body
    rect(px, 3, 10, 3, 13, col)      # left arm
    rect(px, 12, 10, 12, 13, col)    # right arm
    if trim:
        rect(px, 4, 13, 11, 14, trim)  # belt/trim

def face(px, skin=SKIN):
    rect(px, 4, 3, 11, 9, skin)

def eyes(px, y=6):
    dot(px, 6, y, EYE); dot(px, 9, y, EYE)

def helmet(px, col, plume=None):
    rect(px, 4, 2, 11, 5, col)        # dome
    rect(px, 3, 4, 3, 8, col); rect(px, 12, 4, 12, 8, col)  # cheek guards
    rect(px, 5, 6, 10, 6, col)        # nose guard band leaves a slit below
    if plume:
        rect(px, 7, 0, 8, 3, plume)

def pointy_hat(px, col, brim=None):
    rect(px, 7, 0, 8, 0, col)
    rect(px, 6, 1, 9, 2, col)
    rect(px, 5, 3, 10, 4, col)
    if brim:
        rect(px, 3, 5, 12, 5, brim)

def hood(px, col):
    rect(px, 4, 2, 11, 5, col)
    rect(px, 3, 4, 3, 9, col); rect(px, 12, 4, 12, 9, col)
    rect(px, 4, 3, 5, 4, col); rect(px, 10, 3, 11, 4, col)  # frame the face

def sword(px, blade=C(200,210,225), hilt=C(150,110,60)):
    rect(px, 14, 8, 14, 13, blade)
    rect(px, 13, 13, 15, 13, hilt)
    dot(px, 14, 7, blade)

def staff(px, wood=C(150,110,60), gem=C(120,200,255)):
    rect(px, 13, 8, 13, 16, wood)
    rect(px, 12, 7, 14, 7, gem); dot(px, 13, 6, gem)

def axe(px, blade=C(190,200,210), wood=C(150,110,60)):
    rect(px, 13, 9, 13, 15, wood)
    rect(px, 12, 8, 14, 10, blade); dot(px, 14, 9, blade)

def bow(px, wood=C(150,110,60)):
    rect(px, 13, 7, 13, 14, wood)
    dot(px, 12, 7, wood); dot(px, 12, 14, wood)
    rect(px, 14, 8, 14, 13, C(220,220,220))  # string

# ---- heroes ----
def knight(primary, plume):
    im = canvas(); px = im.load()
    legs(px, C(70,70,90)); torso(px, primary, trim=C(210,180,60))
    face(px); helmet(px, C(170,178,190), plume=plume)
    # face slit
    rect(px, 5, 7, 10, 8, SKIN); eyes(px, 7)
    sword(px)
    return outline(im)

def mage(robe, hat, gem):
    im = canvas(); px = im.load()
    legs(px, C(60,55,90)); torso(px, robe, trim=C(255,220,90))
    face(px); eyes(px, 6)
    pointy_hat(px, hat, brim=hat)
    staff(px, gem=gem)
    return outline(im)

def archer(tunic, hoodc):
    im = canvas(); px = im.load()
    legs(px, C(80,60,40)); torso(px, tunic, trim=C(110,80,50))
    face(px); eyes(px, 6); hood(px, hoodc)
    bow(px)
    return outline(im)

def rogue(cloak):
    im = canvas(); px = im.load()
    legs(px, C(40,40,55)); torso(px, cloak)
    face(px, C(225,185,150)); hood(px, cloak)
    dot(px, 6, 6, EYE); dot(px, 9, 6, EYE)
    sword(px, blade=C(180,190,205))   # dagger-ish
    rect(px, 14, 11, 14, 13, (0,0,0,0))
    return outline(im)

def barbarian(skin=SKIN):
    im = canvas(); px = im.load()
    legs(px, C(110,80,50)); torso(px, C(150,110,70), trim=C(90,60,40))
    face(px, skin)
    rect(px, 4, 2, 11, 3, C(120,70,40))  # hair
    eyes(px, 6)
    axe(px)
    return outline(im)

def skeleton():
    im = canvas(); px = im.load()
    legs(px, BONE); torso(px, C(120,125,135))
    face(px, BONE)
    dot(px, 6, 6, EYE); dot(px, 7, 6, EYE); dot(px, 9, 6, EYE); dot(px, 10, 6, EYE)
    rect(px, 6, 8, 9, 8, EYE)  # teeth line
    sword(px)
    return outline(im)

def goblin():
    im = canvas(); px = im.load()
    legs(px, C(80,110,70)); torso(px, C(120,90,60), trim=C(80,60,40))
    face(px, SKIN2)
    dot(px, 3, 3, SKIN2); dot(px, 12, 3, SKIN2)  # ears
    rect(px, 3, 4, 3, 4, SKIN2); rect(px, 12, 4, 12, 4, SKIN2)
    dot(px, 6, 6, EYE); dot(px, 9, 6, EYE)
    axe(px, blade=C(140,140,150))
    return outline(im)

def paladin():
    im = canvas(); px = im.load()
    legs(px, C(120,100,40)); torso(px, C(235,205,90), trim=C(255,235,150))
    face(px); helmet(px, C(240,210,90))
    rect(px, 5, 7, 10, 8, SKIN); eyes(px, 7)
    sword(px, blade=C(255,245,200))
    return outline(im)

HEROES = {
    "knight-red":   knight(C(180,60,60),  C(220,70,70)),
    "knight-blue":  knight(C(70,90,200),  C(90,120,230)),
    "knight-green": knight(C(70,150,90),  C(90,180,110)),
    "mage-purple":  mage(C(120,70,180),   C(95,55,150),  C(180,130,255)),
    "mage-blue":    mage(C(60,90,180),     C(45,70,150),  C(130,210,255)),
    "archer":       archer(C(80,130,70),   C(60,100,55)),
    "rogue":        rogue(C(60,60,80)),
    "barbarian":    barbarian(),
    "skeleton":     skeleton(),
    "goblin":       goblin(),
    "paladin":      paladin(),
}

import os, sys
outdir = sys.argv[1] if len(sys.argv) > 1 else "/tmp/heroes"
os.makedirs(outdir, exist_ok=True)
for name, im in HEROES.items():
    im.save(os.path.join(outdir, name + ".png"))

# contact sheet (scaled 10x) for review
names = list(HEROES)
cols = len(names)
SC = 10
sheet = Image.new("RGBA", (cols * (W+2) * SC, (H+6) * SC), (40, 44, 56, 255))
for i, name in enumerate(names):
    big = HEROES[name].resize((W*SC, H*SC), Image.NEAREST)
    sheet.alpha_composite(big, (i * (W+2) * SC + SC, SC))
sheet.save(os.path.join(outdir, "_contact.png"))
print("wrote", len(names), "heroes to", outdir)

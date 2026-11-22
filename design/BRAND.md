# Nodach brand rules

This file is the master copy of Nodach's brand rules (FIN-3). The tokens it names live in `tokens.json` beside it. Changing anything under `design/` is high risk: an AI reviewer checks the change first, then Nowed approves the merge (TOK-3).


Shared nodes, under one roof. This is the brand book for Nodach: Nodach Studio, nodach.com and every product Nodach fully owns. It is version 1, settled with Nowed on 21–22 November 2026, and every change after this is a new version in this system's history.

## The name

Nodach is built from three layers, and all three are true:

- **node**: the unit everything in Nodach is made of. Ideas, actions, people, settings and agreements are all nodes on one graph.
- **dach**: German and Polish for *roof*. One roof over every venture, profile and community.
- **-ach**: in Irish and Scottish Gaelic, a suffix meaning *of* or *belonging to* (as in *Albannach*, "of Alba"). Nodach: *of the nodes*, belonging to the nodes.

Say it as one story: **the roof over shared nodes**. Don't claim "-ach" means "our"; it doesn't in any language we know of. In German, "Nodach" can be heard as "no Dach" (no roof). The layered story answers that, so tell the whole story rather than the roof alone.

## Voice

Calm and technical. Short sentences, plain words, exact numbers. Explain what a thing does before what it means. No hype, no exclamation marks, no emoji.

## The mark

The mark is a block N cut by a wave: a wide rectangle with two cuts, one from the top and one from the bottom, leaving a single stepped band. Files are in the Logos group.

Rules:

1. **The N never changes shape.** It keeps the same width and proportions (240 : 100) everywhere. Never redraw it squarer, never crop it, never stretch it. In square or round spaces (app icons, avatars, favicons) the container takes the extra space and the mark sits centred inside it.
2. **Two colours only.** Cream on `nodach-green`, or green on `nodach-cream`. Black or white only where one colour is all the medium allows: fax, stamp, engraving, embroidery, laser-cut.
3. **Clear space** on every side equals half the mark's height. Nothing enters it: no text, no edges, no other logos.
4. **Minimum size** is 24 px wide on screen and 10 mm in print. The 16 px favicon is the one exception, and it always sits on a `nodach-green` tile (`radius-sm`).
5. **Busy or green-ish backgrounds:** put the mark on its own `nodach-green` tile, or switch it to cream. Green on a similar green fails (see Test 2 on the brand canvas).
6. **The N is a brand mark only.** It never appears as a node, a button or an icon inside the product. Nodes stay plain.

## The wordmark

`nodach` in lowercase, `wordmark` style (Space Grotesk 700, −3% tracking), in `nodach-green`, the same colour as the mark, so the lockup reads as one unit (decided 22 Nov, CHV-2). On `nodach-green` and on dark-theme backgrounds the whole lockup turns `nodach-cream` (green on the dark `surface` is only 1.9:1). It sits to the right of the mark, with the gap equal to half the mark's height. Its cap height lines up with the top of the mark.

A product name goes underneath as a descriptor: `STUDIO`, `DESKTOP`, `FOR CLAUDE`. Use the `descriptor` style, uppercase, widely spaced, and left-aligned with the wordmark.

## Navigation bar

Settled in Figma on 22 Nov (D-nav-hover):

- **Collapsed by default**: the N mark on the left, three node dots in the centre as the hint, and the avatar on the right. There is no bar background and no divider, and the bar stays transparent when the page scrolls.
- **Opens** when the pointer rests on the bar for `duration-nav-open-delay`, fading in over `duration-nav-open` with `easing-out` (reveal R4). It closes `duration-nav-close-delay` after the pointer leaves, fading out over `duration-nav-close` with `easing-in`.
- **Open**: the all-green lockup, the sections, search and the avatar.
- **Section hover**: a `nodach-green` underline grows from the centre over `duration-hover` (H1).
- **Touch**: tapping the bar opens and closes it. **Keyboard**: focusing anything in the bar opens it. **Reduced motion**: no fade.

## Naming and architecture

Naming follows ownership.

- **What Nodach fully owns** is called `nodach ___`: nodach studio, nodach desktop, nodach for claude, and nodach genjob while genjob is still a trading name.
- **A venture with co-owners or outside investment** gets its own name and brand. Clanstack is the first.
- **"Hosted on nodach"** appears only inside Studio, never on the venture's own pages.

## Colour

- `nodach-green` and `nodach-cream` are the brand: use them big and flat. Interface colour comes from `surface`, `surface-raised`, `ink` and `ink-muted`.
- `danger` means errors and nothing else.
- Every text pairing in this system meets 4.5:1 contrast in both themes. Cream on green is about 9:1.

## Type

- **Space Grotesk** for everything people read: the wordmark, headings and covers (`display`), and body text and the interface (`sans`). One family keeps the brand recognisable at every size.
- **JetBrains Mono** (`mono`) for IDs and codes such as `R-261122-brand-6`, `D-brand-architecture` and `NDR-3c`. These are how Nowed and his tools address everything, so give them their own face.

Both are open-source (SIL Open Font License) and served from Google Fonts. You can self-host and bundle them in the AGPL repo.

## Components

Three product components so far, in the Product group: **Button**, **NodeCard** and **StudioHeader**. Each one has its own guidelines and a live preview. Build new parts of Studio from these and the tokens, not from one-off styles.

## Iconography

Product icons are 1.5 px stroke line icons on a 24 px grid, with round caps, in `ink` or `on-brand`. No filled icon set and no emoji. The N mark is never used as an icon (rule 6).

## Where this was decided

Mirrors that copy from this file: the Claude design system https://claude.ai/artifact/1z4jNEfHKTEN2WUM7Caz8Y and Figma https://www.figma.com/design/PVzf4eny8svQn1mGs7pg40.

The brand canvas holds the four directions, the 12 N options, the locked lockup and the stress tests: https://claude.ai/artifact/3LRvXiSwgd8nu7A2m6XiM8. Decisions are recorded in the Nodach Studio hub as `D-brand-identity`, `D-brand-architecture`, `RULE-brand-n-fixed` and `RULE-brand-canvas-owner`.

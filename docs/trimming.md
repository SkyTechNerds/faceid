# Photo trimming — how FaceID keeps a gallery healthy

> **TL;DR** — Recognition quality depends on *diverse* reference photos, not *many*.
> When a person goes over the photo limit, FaceID sets aside the photo that is **most
> similar to all the others** (the most redundant one), so unusual angles are always
> kept. Nothing is deleted silently — set-aside photos stay visible on the person card
> and can be restored with one click.

## Why a limit at all?

Every reference photo you assign to a person becomes a point the recognizer compares
against. More points is **not** automatically better:

- **Imbalance biases matching.** If Alice has 200 photos and Bob has 5, a borderline
  face is far more likely to be called "Alice" — simply because she has more chances to
  score a lucky match from an odd angle. A cap keeps people on a level playing field.
- **Redundancy adds nothing.** Thirty near-identical front shots don't help recognition
  more than five do — they just take up the budget that a side profile or a low-light
  shot could fill.

So FaceID bounds each person to a photo cap (default **40**, adjustable in **Settings →
Max photos per person**) and spends that budget on *variety*.

## What gets trimmed, and why that's the right one

When adding a photo would push a person over the cap, FaceID computes, for every photo,
how similar it is to all the others of that person, and sets aside the one with the
**highest average similarity** — i.e. the most redundant.

Concretely (real numbers from a test gallery, similarity 0 = unique … 1 = identical):

| Photo | avg. similarity | fate |
|---|---|---|
| rare side angle | 0.04 | **kept** (very different from the rest) |
| unusual lighting | 0.05 | **kept** |
| another front shot | 0.34 | **set aside first** (redundant) |
| yet another front shot | 0.34 | set aside next |

The effect: your 30th near-identical front photo is the first to go, while the one good
profile shot is protected. This is exactly what you want for recognizing someone from
different viewpoints.

There's a second safety net on the way *in*: a genuinely new photo is only stored if it
adds something — a near-duplicate of one you already have is skipped before it's ever
saved.

## Nothing disappears silently

Trimmed photos are **not deleted**. They move to a "set aside" area shown on the person's
card (desaturated, under a `✂︎ SET ASIDE` label). For each one you can:

- **↩ Restore** — bring it back into the active gallery (allowed even if it briefly puts
  the person over the cap; you decide, not the algorithm).
- **× Delete** — remove it for good, once you're sure it's redundant.

Lowering the cap in Settings trims everyone down to the new number **immediately**, so
you can see exactly what moved and undo any choice you disagree with. Raising the cap
does *not* auto-restore — bring photos back yourself, so nothing reappears unexpectedly.

## Practical tips

- **Poor recognition from one angle?** Add a photo taken from *that* angle. Being unusual,
  it scores low similarity to the rest and is automatically kept.
- **Want more/fewer references per person?** Change **Max photos per person** in Settings.
  Higher keeps more variety but costs a little RAM/CPU; the default of 40 is plenty for
  a household.
- **Curating by hand?** Restore the set-aside photos you want, delete the ones you don't —
  the cap only acts on *new* additions, so a gallery you've hand-tuned stays as you left it
  until you add more.

## It won't pile up forever

Set-aside photos are capped too: FaceID keeps only the most recent **`trimmed_keep`** per
person (default 10, in Settings) and deletes older ones. So even after years of a person
walking past the camera, the set-aside area stays small. A **clear all** button per person
removes them on demand.

Note: recognizing a face live does **not** add it to the gallery — only *you* do, by
assigning or uploading. Walking past the camera costs nothing in storage.

## Under the hood

- Similarity is cosine similarity between L2-normalized ArcFace embeddings — the same
  512-dimensional face vectors used for recognition itself.
- "Most redundant" = highest mean cosine similarity to the person's other embeddings.
- Set-aside photos and their embeddings live in `data/persons/<slug>/_trimmed/`
  (`log.json` records the reason, timestamp and similarity), so a restore is exact — the
  original embedding is put back, not recomputed.

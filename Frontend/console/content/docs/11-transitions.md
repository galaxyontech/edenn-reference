---
title: Transitions reference
description: All 58 transition effects available to the image soundtrack endpoint.
---

Every value accepted by `transition_types`. **Names are case-insensitive.** An unrecognised name returns `400` with the full list of valid values.

For how per-change lists work, see [Image soundtrack](../image-music/#how-transitions-work).

The 16 effects marked **W** are in the weighted random pool that `transition_mode=auto` — the default — draws from. It favours restrained blends over showy wipes. All 58 are available when you name them explicitly in `transition_types`.


## Fades and dissolves

| Name | Effect | Pool |
|---|---|---|
| `fade` | Crossfade (dissolve one frame into the next) | **W** |
| `fadeblack` | Fade out to black, then in from black | **W** |
| `fadewhite` | Fade out to white, then in from white | **W** |
| `fadegrays` | Fade through grayscale into the next frame |  |
| `fadefast` | Crossfade weighted toward a fast start |  |
| `fadeslow` | Crossfade weighted toward a slow start |  |
| `dissolve` | Noise-masked pixel dissolve (grainy hand-off) | **W** |
| `pixelize` | Mosaic out, then resolve back in | **W** |
| `distance` | Distance-field based blend |  |
| `hblur` | Blur out, swap, blur back in |  |


## Hard-edged wipes

| Name | Effect | Pool |
|---|---|---|
| `wipeleft` | Hard-edged wipe moving left | **W** |
| `wiperight` | Hard-edged wipe moving right | **W** |
| `wipeup` | Hard-edged wipe moving up |  |
| `wipedown` | Hard-edged wipe moving down |  |
| `wipetl` | Hard-edged wipe from the top-left corner |  |
| `wipetr` | Hard-edged wipe from the top-right corner |  |
| `wipebl` | Hard-edged wipe from the bottom-left corner |  |
| `wipebr` | Hard-edged wipe from the bottom-right corner |  |


## Soft-edged wipes

| Name | Effect | Pool |
|---|---|---|
| `smoothleft` | Soft-edged wipe moving left | **W** |
| `smoothright` | Soft-edged wipe moving right | **W** |
| `smoothup` | Soft-edged wipe moving up | **W** |
| `smoothdown` | Soft-edged wipe moving down | **W** |


## Slides

| Name | Effect | Pool |
|---|---|---|
| `slideleft` | New frame slides in, pushing the old one left | **W** |
| `slideright` | New frame slides in, pushing the old one right | **W** |
| `slideup` | New frame slides in, pushing the old one up |  |
| `slidedown` | New frame slides in, pushing the old one down |  |
| `coverleft` | New frame slides over the old one, moving left |  |
| `coverright` | New frame slides over the old one, moving right |  |
| `coverup` | New frame slides over the old one, moving up |  |
| `coverdown` | New frame slides over the old one, moving down |  |
| `revealleft` | Old frame slides away left to reveal the new one |  |
| `revealright` | Old frame slides away right to reveal the new one |  |
| `revealup` | Old frame slides away up to reveal the new one |  |
| `revealdown` | Old frame slides away down to reveal the new one |  |


## Geometric

| Name | Effect | Pool |
|---|---|---|
| `circleopen` | Circular reveal expanding from the center | **W** |
| `circleclose` | Circular reveal contracting to the center | **W** |
| `circlecrop` | Circular crop closes, then opens on the new frame |  |
| `rectcrop` | Rectangular crop closes, then opens on the new frame |  |
| `radial` | Radial clock-wipe around the center | **W** |
| `vertopen` | Two vertical halves open outward |  |
| `vertclose` | Two vertical halves close inward |  |
| `horzopen` | Two horizontal halves open outward |  |
| `horzclose` | Two horizontal halves close inward |  |


## Diagonal

| Name | Effect | Pool |
|---|---|---|
| `diagtl` | Diagonal wipe from the top-left |  |
| `diagtr` | Diagonal wipe from the top-right |  |
| `diagbl` | Diagonal wipe from the bottom-left |  |
| `diagbr` | Diagonal wipe from the bottom-right |  |


## Slices and wind

| Name | Effect | Pool |
|---|---|---|
| `hlslice` | Horizontal left slice reveal |  |
| `hrslice` | Horizontal right slice reveal |  |
| `vuslice` | Vertical up slice reveal |  |
| `vdslice` | Vertical down slice reveal |  |
| `hlwind` | Horizontal left wind-streak reveal |  |
| `hrwind` | Horizontal right wind-streak reveal |  |
| `vuwind` | Vertical up wind-streak reveal |  |
| `vdwind` | Vertical down wind-streak reveal |  |


## Zoom and squeeze

| Name | Effect | Pool |
|---|---|---|
| `squeezeh` | Horizontal squeeze |  |
| `squeezev` | Vertical squeeze |  |
| `zoomin` | Zoom into the new frame |  |


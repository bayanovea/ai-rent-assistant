# What we are looking for

This file is pasted whole into every AI batch. Changing the priorities means
editing this Markdown, not the code.

The tracked copy is a sample. Put your own letter at
`$RENT_ASSISTANT_HOME/requirements.md` and it is used instead; the district
table and the automatic rejections below are the ones the code enforces, so
keep those sections in whatever letter you write.

## Letter to the agent (verbatim)

> Hello! We are two adults looking for a long-term rental on Phuket. We have
> a small dog, so pets must be allowed — this is non-negotiable for us. We
> plan to move in on 1 December 2026 and want a one-year contract. Budget is
> 15,000–35,000 THB per month.
>
> We are looking for a house or townhouse in a good gated village. It must be
> fully furnished, with at least one bedroom plus a separate living room and a
> proper kitchen (not a tiny kitchenette). A green area nearby where we can
> walk the dog matters. We need parking for one car and good, stable
> internet.
>
> We do not need a tourist area or the beach — we live here, we are not on
> holiday. Kathu, Bang Tao and Boat Lagoon suit us best; Rawai, Nai Harn and
> the airport area are fine. We do not consider Patong at all.

## Priority checklist

1. **Pets allowed — criterion #1.** An explicit ban is an automatic
   rejection. No information is not a rejection, but a mandatory question
   for the agent.
2. Budget 15,000–35,000 THB/month. 35,001–40,000 is considered with a
   remark. Above 40,000 is an automatic rejection.
3. Living area from 35 m². Smaller is an automatic rejection. An unstated or
   unclear size is not a rejection.
4. A house or townhouse in a gated village. A condo or apartment is not a
   rejection, just lower priority.
5. One-year contract, move-in by 1 December 2026.
6. At least 1 bedroom plus a separate living room. **More rooms is a plus,
   not a minus.** A living room that is not mentioned is a yellow flag, not
   a rejection.
7. Fully furnished.
8. A proper kitchen (not a kitchenette).
9. A green area nearby to walk the dog.
10. Parking.
11. Good internet.

## Districts (1–5 scale, applied by the code)

The district rating is added to the score: the pivot is 3.5, and every half
point above or below it is worth 3 points. Patong is an automatic rejection.

| Rating | Districts |
| --- | --- |
| 5 | Kathu, Bang Tao, Boat Lagoon (the marina in Koh Kaew — name it as the district, not the broader Koh Kaew, when the ad names it) |
| 4.5 | Cherngtalay, Laguna, Surin, Boat Avenue, Layan |
| 4 | Rawai, Nai Harn, the airport area (Nai Yang, Nai Thon, Mai Khao, Sakhu), central Phuket Town (Old Town, Talat Nuea, Talat Yai) |
| 3.5 | Kata, Karon (too touristy), inland Thalang (Si Sunthon, Bang Jo, Koh Kaew other than Boat Lagoon, Pa Khlok), Kamala, Panwa, Phuket Town without detail |
| 3 | Chalong, Wichit, Samkong, Ratsada, Sapam, Ao Po |
| 1 | Patong — automatic rejection |

An unnamed district is not a minus: unknown is worth 0 points.

## What we do NOT require

- Proximity to the beach or to tourist areas. Being near the sea is a nice
  bonus, not a criterion: it is already reflected in the district ratings
  above, and nothing gives separate points for the distance to the beach.
- A pool or a gym — a nice bonus, not a criterion.

## Automatic rejections (applied by the code, not by the AI)

- Pets explicitly forbidden.
- District Patong.
- Price above 40,000 THB/month.
- An explicit studio (`property_type = studio` or `bedrooms = 0`).
- A stated living area under 35 m² (`size_sqm < 35`). An empty size is not a
  rejection.

Everything else is a remark in the verdict; the listing stays active.

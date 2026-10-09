# Submission draft (tokensand.com/vastnyc, "Submit your project")

**Project name:** Bountiful on VAST: every market, searchable

**One-liner:** Film a market once; every one-of-a-kind item in it becomes a searchable shop listing,
catalogued by NVIDIA Cosmos on CoreWeave GPUs and stored in VAST.

**What we built:**
The best things in the world are sold in places with no online store: a carver's stall in Ban Tawai, a
furniture street in Kerobokan, a beetle-specimen table at the Chiang Mai night market, a vintage-lamp
dealer at Chelsea Flea. Bountiful turns a walkthrough video of those places into a shop.

Today we ran four markets (Bali, two in Chiang Mai, and Chelsea Flea in New York) through a pipeline on
the event stack: the video lands in VAST S3, is cut into 5-second pieces, NVIDIA Cosmos Reason lists what is
for sale in each piece, Llama 3.3 70B on Weights & Biases Inference turns that into clean, deduplicated
catalogue entries, Cosmos Embed1 indexes every item and every moment, and everything is written to VastDB.
The result is a browsable, searchable shop where every listing links back to the exact second it appears
on camera.

We also benchmarked it against Bountiful's existing hand-tuned pipeline on the same footage (Compare tab):
the VAST stack catalogued a market in under a minute of compute per 30 seconds of video, and we show item by
item what it found and missed.

**Numbers (measured today, shared GPUs):** 97 catalogue items across 4 markets; Kerobokan 16 items in 44 s,
Ban Tawai 29 in 97 s, Chelsea Flea 30 in 117 s, Chiang Mai 22 in 88 s. Cosmos Reason 2.5 to 4 s per
5-second piece. About 100 s of compute per minute of video on one worker; pieces are independent, so 1,000
shops x 5 minutes is ~2.8 hours on 50 workers.

**Tools used:** VAST AI OS (S3, VastDB, VSS search), NVIDIA Cosmos Reason + Cosmos Embed1, CoreWeave GPUs,
Weights & Biases Inference (Llama 3.3 70B), YOLO11, ffmpeg, Python.

**Repo:** github.com/MatthewMakesMagic/bountiful-vast (pending your OK to publish)
**Demo video:** (link after recording)
**Team:** Matthew, Chris (team 28)  <- add full names + contact emails

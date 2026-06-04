# WaveSense — Pitching Script
### UM Technothon · Team Indecisive · 12-Minute Presentation

---

## Script Framework Overview

| # | Slide | Section | Time | Cumulative |
|---|-------|---------|------|------------|
| 1 | Title | Hook & Introduction | 0:45 | 0:45 |
| 2 | The Problem | Pain Points & Stakes | 1:30 | 2:15 |
| 3 | The Solution | WaveSense Positioning | 1:15 | 3:30 |
| 4 | Technology | How It Works (Technical) | 1:30 | 5:00 |
| 5 | Live Demo | Demo Walkthrough | 2:30 | 7:30 |
| 6 | Enterprise Value | Revenue & Use Cases | 1:00 | 8:30 |
| 7 | Architecture | Reliability & Security | 0:45 | 9:15 |
| 8 | Cost & Scalability | Pricing & Market Fit | 1:00 | 10:15 |
| 9 | Closing | Summary & Call to Action | 1:45 | 12:00 |

---

## Slide-by-Slide Exact Wording

---

### SLIDE 1 — Title (0:45)
**[Advance to slide. Pause 2 seconds. Make eye contact with the audience.]**

> "Every university building in Malaysia is running its air-conditioning and lights right now — in rooms where nobody is sitting.
>
> We're here to fix that.
>
> My name is [Name], and this is Team Indecisive. Our product is **WaveSense** — invisible presence detection for the zero-waste smart building.
>
> In the next twelve minutes, we're going to show you a working system that turns the Wi-Fi router your building already has into a presence sensor — no new hardware, no cameras, and no privacy risk whatsoever.
>
> Let's get into it."

---

### SLIDE 2 — The Problem (1:30)
**[Advance slide. Let the stats land for a moment before speaking.]**

> "Buildings are bleeding energy — and the numbers are alarming.
>
> Between 30 and 40 percent of commercial energy is wasted on spaces that nobody is actually using. Across Malaysian universities alone, that translates to over a hundred million ringgit drained every single year.
>
> And here is the core reason why: existing solutions simply don't work.
>
> PIR motion sensors — the kind you see in every office corridor — detect movement. They do not detect presence. The moment a student sits down to study silently, the sensor declares the room empty and shuts off the AC. The student is still there. The system doesn't know.
>
> What about cameras? Building managers reject them outright. They're expensive to install — we're talking fifty thousand ringgit per floor — they raise serious PDPA compliance concerns, and frankly, nobody wants to be filmed while they work.
>
> And on top of all that: ghost bookings. Meeting rooms that are booked, air-conditioned, and completely empty because no one showed up. There is no automatic way to reclaim them.
>
> Three broken systems. One underlying gap: nobody actually knows if a person is in the room."

---

### SLIDE 3 — The Solution (1:15)
**[Advance slide. Gesture at the comparison table.]**

> "This is where WaveSense comes in.
>
> Instead of adding new hardware, we repurpose the Wi-Fi infrastructure that every building already has.
>
> Wi-Fi signals — 802.11 frames — are constantly passing through every room. When those signals travel through a human body, they distort. We capture that distortion. And from it, we can tell whether a room is occupied or empty — even if the occupant is sitting perfectly still.
>
> Look at this comparison. Traditional PIR on the left — requires movement, fails stationary users, no volumetric data. Camera systems in the middle — privacy invasive, fifty thousand ringgit to install, refused by managers.
>
> WaveSense on the right — detects breathing and stillness, one hundred percent stationary detection, a rich API data stream, anonymous by physics, and approximately three hundred and eighty ringgit per floor.
>
> We are not a better sensor. We are a fundamentally different category of product."

---

### SLIDE 4 — Technology (1:30)
**[Advance slide. Speak with confidence — this is your technical credibility moment.]**

> "Let me explain exactly how it works — because the physics here is the product moat.
>
> Every Wi-Fi transmission carries what's called Channel State Information, or CSI. This is the signal-level data describing how the radio wave changed as it traveled from transmitter to receiver. When a human body — which is roughly 60 percent water — is in that path, the wave distorts in a measurable, repeatable way.
>
> Our ESP32-TX broadcasts standard 802.11 frames at a hundred times per second. The ESP32-RX captures the CSI from each of those packets and immediately publishes the amplitude data as a JSON payload over MQTT.
>
> On the backend, a Python process computes a rolling variance across 52 OFDM subcarriers — the individual frequency channels within a single Wi-Fi transmission. An empty room gives you a low, flat variance. A human present — even just breathing — produces elevated, dynamic variance that is statistically distinct from noise.
>
> We add hysteresis filtering: the system must see sustained elevated variance before declaring the room occupied, and sustained low variance before declaring it empty. This eliminates phantom triggers from passing traffic or a single packet anomaly.
>
> The output goes straight to a relay. HVAC on. Lights on. Human leaves. Variance drops. Relay clicks off. It really is that direct."

---

### SLIDE 5 — Live Demonstration (2:30)
**[Advance slide. Move to the hardware. Speak calmly and let the system do the talking.]**

> "Now let's see this working in real time. I'm opening the Flask dashboard at localhost:5000 — this is running entirely on the laptop sitting right here, no cloud dependency for core detection.
>
> [Demo beat 01 — Wave hand near ESP32s]
>
> Watch the CSI variance chart at the top. I'm moving my hand near the ESP32 pair — and you can see the variance spiking across those 52 subcarrier channels in real time. The room status in the top right has just flipped from EMPTY to OCCUPIED, and the relay LED has turned on.
>
> [Demo beat 02 — Stand completely still]
>
> Now I'm going to stand completely still. No movement at all. Just breathing.
>
> Notice the relay LED is still on. The room is still OCCUPIED. The system is detecting my breathing — the micro-movement of my chest — as enough variance to hold the occupied state. This is what PIR sensors cannot do. A student studying silently would keep this room conditioned indefinitely.
>
> [Demo beat 03 — Ghost booking]
>
> Now I'm stepping away entirely. Fifteen seconds of empty detection... and there it is — the dashboard prints 'Meeting cancelled — room freed'. That was a mock Google Calendar REST API call. In a production deployment, that call goes to the real Google Workspace or Outlook API and immediately releases the booking so another team can use the room.
>
> [Demo beat 04 — Google Home integration]
>
> And here's where it gets interesting. Watch my phone — I'm pulling up the Google Home app. You can see a 'Room Occupancy' switch right here alongside my other smart devices. When the room is occupied, it shows ON. When empty, it shows OFF. And if I ask — *Hey Google, is the room occupied?* — it tells me instantly. We built this using Google's Smart Home Action API with real-time state reporting via the Home Graph API. No extra hardware. The same ESP32 data that controls the relay also feeds Google Home.
>
> [Demo beat 05 — Heatmap tab]
>
> Finally, switching to the Heatmap tab. This accumulates occupied-room-minutes over the course of the day. As usage builds up, each room transitions from CLEAN, to MODERATE, to DIRTY — not on a fixed schedule, but based on actual footprint. Cleaners get sent only where rooms have genuinely been used.
>
> One sensor grid. Five live applications. All running on off-the-shelf hardware."

---

### SLIDE 6 — Enterprise Value (1:00)
**[Advance slide. Speak with energy — this is the commercial case.]**

> "So what are we actually selling?
>
> The five applications you just saw — HVAC and lighting control, ghost booking cancellation, Google Home smart integration, janitorial routing, and after-hours security alerts — are the five revenue streams that a building management system can subscribe to.
>
> But the true product is the WaveSense Occupancy Data API. A real-time feed of which rooms are occupied, for how long, and with what usage patterns — streamed to any building system that can consume a REST or MQTT endpoint.
>
> Think about what that means for a smart building vendor. Right now their platform has no reliable occupancy signal for stationary users. We give them that signal. Instantly. Via a REST call. And because we integrate directly with Google Home, any facilities manager can check room occupancy from their phone — no custom dashboard required."

---

### SLIDE 7 — Architecture & Reliability (0:45)
**[Advance slide. Be concise — this is for technical credibility, not a deep dive.]**

> "A quick word on production readiness.
>
> The system runs on MQTT port 1883 for the raw ESP32 telemetry pipeline, and Flask on port 5000 for the variance math, dashboard, B2B API endpoints, and Google Home Smart Home Action fulfillment. Google Home integration uses the official Home Graph API for real-time state reporting — no third-party bridges required.
>
> Two critical safety features. First: the relay is wired to the normally-closed port. If WaveSense ever loses power, the relay mechanically snaps shut — instantly restoring legacy thermostat control. The building's HVAC never breaks.
>
> Second: one hundred percent of execution is local. Zero port forwarding to the internet. If the building fibre is cut, our MQTT broker and Python backend continue operating autonomously. The building keeps running. Google Home queries come through a secure tunnel, but the core detection never depends on cloud connectivity.
>
> And because we measure only the mathematical variance of radio waves, there is no personally identifiable data ever collected. No cameras. No microphones. No MAC addresses. Anonymous by the laws of physics."

---

### SLIDE 8 — Cost & Scalability (1:00)
**[Advance slide. Let the numbers speak — keep this punchy.]**

> "Now the number that matters most.
>
> Traditional sensor grids with cameras, PIR arrays, wiring, and installation cost upwards of fifty thousand ringgit per floor. Per floor.
>
> WaveSense costs approximately three hundred and eighty ringgit per floor — two ESP32 modules at twenty ringgit each from Shopee, one Raspberry Pi 4 at three hundred ringgit from Lazada, and forty ringgit for a relay module and miscellaneous cabling.
>
> That's a hundred and thirty-one times cheaper than the alternative. For an entire twenty-storey building, we're looking at roughly six thousand five hundred ringgit — and that's with no new cabling, because we use the Wi-Fi infrastructure that already exists.
>
> These aren't projected figures. These are marketplace prices from Shopee and Lazada as of today. Every component is available off-the-shelf, tonight, with next-day delivery.
>
> The hardware bill of materials is not the barrier to deployment. It never was."

---

### SLIDE 9 — Closing & Call to Action (1:45)
**[Advance slide. Slow down. This is your closing statement — make it land.]**

> "Here is what we know.
>
> The building already has the sensor. It just doesn't know it yet.
>
> Every router broadcasting Wi-Fi right now is also broadcasting a presence signal. It has been the whole time. We wrote the software to listen to it.
>
> Our proof-of-concept is running today, on this table, with live relay control and real CSI data from real hardware. It is privacy-first by physics — no cameras, no microphones, no identity data collected. It is a hundred and thirty-one times cheaper than what the market currently offers. And it runs on an open-source stack that any building manager, facilities team, or smart building vendor can audit, extend, and deploy.
>
> Our roadmap is straightforward. Pilot in one university building — we'd love to start with UM — and validate the energy savings against a metered baseline. From there, integrate the WaveSense Occupancy API with a BMS or smart-building vendor. And ultimately, a rollout across campus networks and commercial properties nationwide.
>
> The market is ready. The hardware is available. The code is written. The demo is live.
>
> We are Team Indecisive — and we are very, very decided on this.
>
> Thank you."

**[Hold eye contact. Smile. Wait for applause before moving to Q&A.]**

---

## Q&A Preparation Notes

**Likely questions and suggested answers:**

**"What's the range / coverage area per ESP32 pair?"**
> One ESP32 TX-RX pair effectively covers a single room of up to ~50 sqm. For larger open-plan spaces, a second pair extends coverage. Coverage is configurable — more pairs mean more granular zone detection.

**"What about interference from other Wi-Fi networks?"**
> We transmit on a dedicated ESP32-to-ESP32 link on a controlled channel. Because we're measuring relative variance from our own packets rather than the ambient spectrum, external networks contribute to background noise but do not cause false positives — the hysteresis filter handles this.

**"Is this PDPA compliant?"**
> Yes, and by design. We collect no personal data whatsoever. No images, no audio, no identity. The only output is a binary occupied/empty flag plus a variance score — both of which are mathematically derived from radio wave physics, not from any biometric or identifying signal.

**"How does it perform in multi-person rooms?"**
> Currently the system is calibrated for binary presence detection (occupied / unoccupied). Multi-person counting via CSI amplitude analysis is in scope for v2 — the subcarrier data already contains enough signal for this.

**"What if someone hacks the MQTT broker?"**
> The broker runs on a LAN-only port with no internet exposure. For enterprise deployment, we add MQTT authentication and TLS. The air-gapped design means the attack surface is limited to the building's internal network.

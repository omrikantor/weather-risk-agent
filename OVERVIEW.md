# Weather Risk Intelligence Agent

*Overview of the solution (Moveo FDE home assignment)*

**Live demo:** https://weather-risk-agent-gz90.onrender.com (access code sent with this email)  
**Code:** https://github.com/omrikantor/weather-risk-agent  

## The problem

A US logistics company wants to know which of its distribution hubs are most exposed to weather disruption, so it can decide where to invest in resilience. Analysts need to ask questions in plain language (rankings, comparisons, statistics, explanations) and trust the numbers they get.

## What I built

- **A chat agent** that answers the assignment's questions and follow-ups. The language model (OpenAI) only chooses tools and explains results; nine deterministic Python tools compute every number. Code then checks that each number in the answer matches a tool result before the answer is shown.
- **A hub network of 47 US cities** chosen to cover every US region and every hazard type. For a place that is not a hub, the agent compares its 3 nearest hubs, weighing hazard-profile similarity (FEMA county data, 80%) and distance (20%), offers the best one as a stand-in and asks before analyzing it. Places farther from the network than its widest gap (Alaska, Hawaii, Puerto Rico) get no stand-in.
- **A web app**: chat with voice input and read-aloud, a ranking of the 47 hubs with 90% ranges, a detail panel per hub, a seasonal outlook, and the live warnings below.
- **Live alerts (bonus)**: every 15 minutes the app checks active National Weather Service warnings at each hub and posts new or ended warnings to a webhook; they also appear in the app and the chat.

## How risk is measured

- **Warning days per year**: days with an NWS warning at the hub (2006 to 2025). NWS sets warning criteria locally, so a warning means dangerous for that place.
- **Disaster-level days per year** (main ranking): warning days times the share of warning days that fell inside a FEMA major disaster. Severity comes from data instead of hand-picked weights; hurricanes weigh most, and heat counts zero because FEMA declared no heat disasters, which the agent explains.
- Station measurements, hurricane tracks and storm reports explain the scores. Every figure has a 90% range, and tiers are the top, middle and bottom third of the network.

## Contract between the model and the code

Strict JSON schemas for tool inputs and for the final answer; code-side number tracing with one retry, then a visible warning; the model provider is isolated in one module.

## Quality

140 automated tests (no model calls) and 21 evaluation cases run against the real model, with expected values computed by calling the same tools: 21 of 21 pass. The evaluations found and fixed real problems, such as a misread confidence label and two definitions of snowfall used for the same question.

## Data, storage and deployment

Public data (NWS, FEMA, NOAA, Census, Open-Meteo) is processed offline into a read-only SQLite file kept in the repository, so the app needs no database server and runs from a fresh clone. Deployed with Docker on Render, protected by an access code, a rate limit and a daily AI spend cap.

## Assumptions and where to read more

The hub network (47 US cities) is an assumption, and all figures are historical frequencies, not forecasts. README.md explains how to run it; DESIGN.md covers architecture and limitations; DECISIONS.md explains each choice and the options rejected; docs/ai-session holds the full AI session.

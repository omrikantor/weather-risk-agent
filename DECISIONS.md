# Decision Log

This file records the main decisions made while building the system: what the options were, what we chose and why. Several decisions were reversed after looking at real data; those reversals are kept here because they explain why the current method looks the way it does. The design itself is described in [DESIGN.md](DESIGN.md).

## D1. Overall approach: deterministic tools plus a language model that explains

**Options considered**

- A. A single agent with a fixed set of deterministic tools, plain SDK, one model.
- B. A retrieval approach where the model reads raw data or documents and reasons over them.
- C. A multi-agent or graph framework (for example LangGraph) with separate planner, analyst and writer agents.

**Choice: A.**

**Why.** The assignment asks for deterministic scoring and JSON-schema enforcement between the model and the code. With approach A every number comes from code, which can be tested, and the model's job is limited to choosing tools and explaining results, which is what it is good at. Approach B lets the model compute numbers, which cannot be verified. Approach C adds structure we did not need: the questions are answered in one to four tool calls, and a framework would add dependencies and hide the loop that we want to show and control. The agent loop is about 125 lines and easy to read.

## D2. Model provider and model

**Options considered:** Anthropic Claude models and OpenAI models; one model for everything or a small model for routing plus a large model for answers.

**Choice:** OpenAI `gpt-6.2-sol` for everything, with medium reasoning effort.

**Why.** Prepaid credit was available on OpenAI. Using one model keeps behavior predictable and the evaluation results meaningful (a two-model setup doubles the combinations to test). The model is isolated in `app/agent/llm_client.py`, so switching provider means rewriting that one file. Measured cost is about 0.013 USD per answer.

## D3. Data storage: SQLite in the repository

**Options considered:** Postgres, DuckDB, flat files (CSV or Parquet), SQLite.

**Choice:** SQLite, with the built database committed to git.

**Why.** The service only reads data; all writes happen in the offline build. The data is about 49 MB after compaction. SQLite needs no server, works the same on a laptop and in a container, and the committed file means a reviewer can run the app without downloading anything or waiting for a build. DuckDB would also work but adds a dependency without a real benefit at this size. Postgres becomes the right choice when several processes write (shared conversation history, alert history) or when the hub network grows very large.

## D4. How to measure risk (the most important decision)

This went through four versions. Each change was triggered by evidence, not by preference.

**Version 1: weather thresholds with hand-picked hazard weights.** Count days above thresholds (heavy snow, extreme heat, high wind and so on) and combine them with weights such as winter 0.25. Rejected by the user and by us: the weights are opinions, and changing them changes the ranking.

**Version 2: disruption days without weights.** Count every day that crosses any threshold, all days equal. Rejected after the first results: Phoenix and Las Vegas ranked at the top because temperatures over 38 C are normal there (about 106 days a year in Phoenix) and the local infrastructure is built for it. A fixed national threshold does not mean the same thing everywhere, and a hot day counted the same as a hurricane day.

**Version 3: FEMA building loss as a severity weight.** Rejected: property damage is not the same as operational disruption of a distribution hub, and the National Risk Index loss numbers are dominated by how much property exists in a county, not by how often the weather is bad.

**Version 4 (current): NWS warnings for frequency, FEMA major-disaster declarations for severity.**

- National Weather Service warning criteria are set by each local office, so a warning means "dangerous for this place". This fixed the Phoenix problem without us choosing any threshold.
- FEMA declarations show how often a warning day became an actual disaster. The share of warning days inside a declared disaster gives each hazard its severity from data: about 46% for hurricanes, 17% for floods, 5% for winter, 4% for thunderstorms and 0% for heat. Nobody chose these numbers.
- The primary measure is disaster-level days per year (warning days times severity share). Warning days per year are always reported next to it, so a hub with frequent but rarely catastrophic weather does not look safe.

**Thought process on heat.** Heat getting zero disaster-level days looked wrong at first. We checked: FEMA did not declare a single heat disaster in our 30-year window. Rather than invent a severity for heat, we kept the data result and made the system say clearly that this means "no evidence of disaster-level heat", while still reporting heat warning days.

## D5. Rules to keep the method honest

- **No parameter tuned to a ranking.** Every threshold, radius and pattern is in `app/scoring/params.yaml` with its reason. When a value needed a test, the test was decided before looking at the ranking it would produce. For example, the 150 km hurricane radius and 50 km storm radius were accepted because rankings stayed stable across nearby radii (rank correlation 0.83 to 0.98).
- **No hub-specific fixes.** When a single hub looked wrong, we looked for the general cause. Two examples:
  - Phoenix had 758 flash-flood warnings because Maricopa County is huge. The general fix: storm-based warnings count only when the warning polygon contains the hub. Phoenix dropped to 31, and every other large county was corrected the same way.
  - Some winter disasters were missing because FEMA filed them as "Severe Storm". The general fix: match declarations by type or by title keywords, with snowmelt and ice jams excluded from winter because they are floods.
- **No use of our own prior knowledge as data.** Expected answers in the evaluation are computed by calling the tools, never typed in by hand.

## D6. Tiers and uncertainty

**Tiers:** thirds of the network (High, Elevated, Low). Fixed cut-offs (for example "over 1 day is high") would be arbitrary because the measure is new and has no external scale. Thirds are simple to explain and always relative to the client's own network.

**Uncertainty:** exact Poisson ranges for rates, exact binomial ranges for shares, and a conservative combination for disaster-level days. We chose conservative over precise because overstating certainty is worse for an investment decision than a wide range.

**Confidence label:** rule-based (station distance, station completeness, how much modeled data was used), so the same data always gives the same label.

## D7. Station data first, model data as backup

Weather statistics questions ("what share of days in Denver had snowfall last year?") are answered from the nearest major airport station (NOAA GHCN-Daily), and Open-Meteo reanalysis fills missing days. Reanalysis is a model grid: it smooths extremes and does not record measured snowfall depth, peak gusts or observed events such as freezing rain. Stations measure what actually happened but have gaps. The answer always says which source was used.

## D8. Places that are not hubs

**Options considered:** refuse; use the nearest hub; run a live assessment for any address; recommend a similar nearby hub.

**Choice:** recommend a stand-in hub and ask before analyzing it.

**Why.** Refusing is unhelpful, and the nearest hub can be misleading (for Charleston, SC the nearest hub is inland Charlotte, while coastal Jacksonville has the matching hurricane and flood profile). A live assessment for any address would be the best answer but needs a different data pipeline; it is listed as future work. The recommendation weighs hazard similarity (80%) above distance (20%), because the purpose is to find a hub with similar weather, and distance mainly breaks ties.

**Ambiguous names and nicknames.** We first considered adding ZIP codes and a nickname table. We decided this was more than the assignment needs: the model converts nicknames to official names (and says so in the answer), and ambiguity is decided from Census population data (one match over 50,000 people wins, otherwise ask). The evaluation found that the model sometimes decided "Springfield is ambiguous" on its own without calling the tool; the prompt now requires the tool call so the decision comes from data.

**Coverage limit.** Instead of listing excluded states, the limit is the widest gap between neighboring hubs in the network (622 km today). This excludes Alaska, Hawaii and Puerto Rico automatically and adapts if the client adds hubs.

## D9. Contract between model and code

- **Strict tool schemas** generated from Pydantic models, with fixed lists for hub ids and regions.
- **A fixed final answer format** (JSON) instead of free text, so code can check it.
- **Number tracing in code.** The first version asked the model to list every figure with its source. It made answers about 60% longer and slower, and it relied on the model to describe itself. We moved tracing into code: it searches the tool outputs for each number in the text. Output tokens dropped by 45%, and the check became stricter.
- **One retry, then a visible warning.** We chose not to hide or silently drop a failed answer; the user sees what could not be verified.
- **Text cleanup in code.** The prompt asks for plain writing without dashes or ellipses, and code removes any that slip through, so the result does not depend on the model following the style rule.

## D10. Answer length

We considered shortening answers to reduce latency. The user preferred complete answers, because analysts use them for investment decisions. We kept the information and reduced latency in other ways (code-side tracing, cached prompt, trimmed history). Average time is about 10 seconds per answer.

## D11. User interface

- **Plain HTML, CSS and JavaScript** without a framework or build step. The UI is small, and a reviewer can read it directly. Libraries (markdown rendering, HTML sanitizing) and the font are stored in the repository, so the page works without external CDNs.
- **Client view by default, developer view on request.** Early versions showed tool traces and token counts to everyone. The audience is a logistics analyst, so technical details moved behind a developer toggle, which is still available for the demonstration.
- **Seasonal outlook** was added because "what should we prepare for this month?" is a practical question for operations, and it reuses the same warning data. It is labeled as historical frequency, not a forecast.

## D12. Protecting a public deployment

A public URL with a paid model needs limits. We chose three simple ones: an access code, a per-IP rate limit, and a daily spend cap (3 USD by default) that an admin can change while the service runs. The leaderboard and hub details keep working after the cap is reached because they do not use the model.

## D13. What we chose not to build

- **MCP server:** useful but not required; the tool registry would make it a thin wrapper.
- **Score-change alerts:** our measures describe 20 years and barely move day to day, so alerting on score changes would rarely fire. Alerting on live NWS warnings per hub would be more useful and is the recommended next step.
- **Client cost data, live address assessment, Postgres, real authentication:** right for production, not for this assignment.

These are described in more detail in section 11 of [DESIGN.md](DESIGN.md).

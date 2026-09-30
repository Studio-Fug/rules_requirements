# Background: the standards

rules_requirements borrows its structure from the standards that govern
medical device software, because they describe — precisely and with decades of
audit practice behind them — what a traceable verification and validation
argument looks like. This page summarises the parts of those standards the
model reflects and maps each concept to the construct that represents it.

```{admonition} Scope
:class: important

This tool helps you **produce and check traceability evidence**. It does not
make a product compliant with any standard, it is not a quality management
system, and it has not itself been validated as a tool for any regulated use.
Applying it in a regulated context means validating it for your intended use
under your own quality system. Clause numbers below refer to the editions
named and change between editions; always work from the text of the standard
that applies to you.
```

Editions referred to: **IEC 62304:2006+A1:2015**, **ISO 14971:2019**,
**IEC 60601-1:2005+A1:2012+A2:2020**, **ISO 13485:2016**, and **21 CFR 820**.

## IEC 62304 — software life cycle processes

IEC 62304 defines the processes for developing and maintaining medical device
software. The parts that shape this tool:

**Software safety classification (§4.3).** Software systems are classified
**A**, **B** or **C** by the harm they could contribute to once risk control
measures external to the software are taken into account — broadly: no
unacceptable risk / no injury, non-serious injury, and death or serious injury.
The class scales the rigor of the required activities. rules_requirements does not store the class
as a field — record it in the model's `project:` metadata — but its effect is
naturally expressed through the verification levels and test methods you demand
of each requirement.

**Software development (§5).** Development planning (§5.1) establishes the
activities and deliverables. *Software requirements analysis* (§5.2) derives
software requirements from system requirements, includes the risk control
measures implemented in software among them (§5.2.3), and verifies that the
requirements are, among other things, traceable to system requirements and
testable (§5.2.6). Architectural and detailed design (§5.3, §5.4) partition the
software into items. Unit verification, integration testing and system testing
(§5.5–§5.7) verify the software against its requirements, with tests
established for each requirement and their results recorded.

**Software risk management (§7).** Software that can contribute to a
hazardous situation is analysed (§7.1); risk control measures are defined and
implemented in software (§7.2); their implementation is verified (§7.3); and
§7.3.3 asks for documented traceability *from the hazardous situation to the
software item, from the software item to the software cause, from the cause to
the risk control measure, and from the risk control measure to its
verification*. That chain is exactly RISK → MIT → REQ → evidence here, with
`@rr(...)` source annotations connecting requirements to software items.

**Configuration management (§8)** identifies and controls the software
configuration, including which versions were verified; the model and the
evidence live in version control alongside the code, and *staleness* checks
that evidence was recorded against the build in question. **Problem resolution
(§9)** tracks problems to closure; the gap queue and entity notes are inputs to
such a process, not a replacement for it.

## ISO 14971 — risk management for medical devices

ISO 14971 defines the risk management process. Its vocabulary is used
verbatim in the risk model:

- **Harm** — injury or damage to the health of people, or damage to property or
  the environment. **Hazard** — a potential source of harm. **Hazardous
  situation** — a circumstance in which people, property or the environment are
  exposed to one or more hazards. **Risk** — the combination of the probability
  of occurrence of harm and the severity of that harm.
- **Risk analysis (§5)** identifies hazards and hazardous situations (§5.4) and
  estimates the associated risks (§5.5) from severity and probability.
- **Risk evaluation (§6)** compares the estimated risks against the
  acceptability criteria defined in the risk management plan.
- **Risk control (§7).** *Risk control option analysis* (§7.1) considers, in
  priority order, **inherent safety by design** (and manufacture),
  **protective measures** in the device or its manufacturing, and **information
  for safety** (and, where appropriate, training). The measures are implemented
  and their implementation *and effectiveness* verified (§7.2). The **residual
  risk** is then evaluated (§7.3); if it is not acceptable, a benefit–risk
  analysis follows (§7.4); risks introduced by the controls themselves are
  reviewed (§7.5); and the **completeness of risk control** is checked (§7.6).
- The **overall residual risk** is evaluated (§8) and the process reviewed (§9)
  before release, with production and post-production information fed back
  (§10).

A risk here carries `hazard`, `hazardous_situation` and `harm`, an estimated
`severity` and `likelihood`, and a residual estimate. `likelihood` is a single
ordinal scale; if your process decomposes the probability of harm (for example
into the probability of the hazardous situation occurring and the probability of
it leading to harm), record the analysis in the description and enter the
combined estimate. The optional `acceptable_risk_score` threshold is a
deliberately simple stand-in for a risk acceptability matrix: the real criteria
belong in your risk management plan. Each **mitigation** is one risk control
measure, typed by the §7.1 option it represents and implemented by requirements
— so the verification of its effectiveness is the verification of those
requirements.

## IEC 60601-1 — programmable electrical medical systems

IEC 60601-1 sets general requirements for the basic safety and essential
performance of medical electrical equipment. It requires a risk management
process complying with ISO 14971, and its clause 14 addresses **programmable
electrical medical systems (PEMS)**: documentation, risk management planning, a
development life cycle, problem resolution, risk management of the programmable
subsystems, a **requirement specification** (§14.7), architecture (§14.8),
design and implementation (§14.9), **verification** (§14.10) and **PEMS
validation** (§14.11), and control of modifications (§14.12). Since Amendment 1
(2012), clause 14 also calls up processes from IEC 62304 for the software of the
programmable electronic subsystems. For a device developed to these standards,
the user needs, requirements, verification evidence and validation rollup
modelled here are the raw material of the §14.7–§14.11 documentation.

## Design controls — 21 CFR 820.30 and ISO 13485 §7.3

Design controls structure product development around **design inputs**
(requirements, including the needs of the user and patient), **design outputs**,
**design review**, **design verification** (outputs meet inputs), **design
validation** (the device meets user needs and intended uses, under actual or
simulated use conditions), design transfer, design changes and the design
history file. They appear in the US as 21 CFR 820.30(a)–(j) and in ISO 13485:2016
as §7.3.2–§7.3.10; ISO 13485 §7.3.2 additionally asks the design and development
plan to document the methods that ensure traceability of design outputs to
design inputs. (Since
2 February 2026 the FDA's Quality Management System Regulation incorporates ISO
13485:2016 into Part 820 by reference; the design-control concepts are
unchanged.)

The two questions the report answers are the two sides of design controls:
**verification** — did we build the product right? (each requirement, proven
with sufficient rigor) — and **validation** — did we build the right product?
(each user need, rolled up from the requirements that satisfy it and from any
direct validation evidence, such as usability studies).

## Mapping

| Concept | Source (edition-dependent) | In rules_requirements |
| ------- | ------------------------- | --------------------- |
| User needs, intended use | 21 CFR 820.30(c),(g); ISO 13485 §7.3.3, §7.3.7 | `user_needs` (UN); VALIDATED rollup; direct validation evidence |
| Design inputs / software requirements | 21 CFR 820.30(c); ISO 13485 §7.3.3; IEC 62304 §5.2; IEC 60601-1 §14.7 | `requirements` (REQ) |
| System → software requirement decomposition | IEC 62304 §5.2.1 | `refines` |
| Requirements traceable and testable | IEC 62304 §5.2.6 | `requirement-orphan` rule; `method`; UNVERIFIED gaps |
| Risk control measures in software requirements | IEC 62304 §5.2.3, §7.2.2 | `mitigations` (MIT) `implemented_by` requirements |
| Design verification; unit, integration and system testing | 21 CFR 820.30(f); ISO 13485 §7.3.6; IEC 62304 §5.5–§5.7; IEC 60601-1 §14.10 | tagged test evidence → VERIFIED / UNDER-VERIFIED / FAILED |
| Test procedures for each requirement | IEC 62304 §5.7.1 | `test_methods` (TM) with `level` and `procedure` |
| Design validation | 21 CFR 820.30(g); ISO 13485 §7.3.7; IEC 60601-1 §14.11 | user-need rollup; evidence tagged with UN ids |
| Hazard, hazardous situation, harm | ISO 14971 §5.4 | `hazard`, `hazardous_situation`, `harm` |
| Risk estimation | ISO 14971 §5.5 | `severity` × `likelihood`, risk score |
| Risk evaluation | ISO 14971 §6 | `acceptable_risk_score`, `risk-unacceptable` rule (simplified) |
| Risk control option analysis | ISO 14971 §7.1 | mitigation `type`: `inherent`, `protective`, `information` |
| Implementation and verification of risk controls | ISO 14971 §7.2; IEC 62304 §7.3 | mitigation verdict from its requirements' verification |
| Residual risk | ISO 14971 §7.3 | `residual_severity`, `residual_likelihood`, `residual` |
| Completeness of risk control | ISO 14971 §7.6 | `risk-unmitigated`, `mitigation-unimplemented` rules; `high-risk-open` gaps |
| Hazard → cause → control → verification traceability | IEC 62304 §7.3.3 | RISK → MIT → REQ → evidence, plus `@rr(...)` links to software items |
| Traceability of outputs to inputs | ISO 13485 §7.3.2 | `@rr(...)` annotations (implemented in / verified in) |
| Verified configuration | IEC 62304 §8 | artifact identity on evidence; staleness |
| Software safety class | IEC 62304 §4.3 | not a field — record in `project:`; drives the levels you demand |
| Benefit–risk, overall residual risk | ISO 14971 §7.4, §8 | not computed — record the judgement in `residual` and your risk file |

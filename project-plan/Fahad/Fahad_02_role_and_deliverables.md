# Fahad: Station software and integration lead

Own the sensory-test engine, trial sequencing, responses, result calculation, integration checkpoints, and demo coordination. Ali owns station screens; Collins owns station wiring; Salaar owns firmware.

## First task
Implement a station simulator and a test state machine without waiting for the motor.

## Owned deliverables
- Station engine independent of UI and hardware adapters
- Trial records and session result with quality limitations
- Integration checklist and failure log
- Pitch script, backup-video coordination, and submission checklist

## Parallel-working rule
Work against mocks or captured samples until the real dependency is ready. Do not wait idle for wiring, APIs, or screens. Flag interface changes to the affected owner immediately.

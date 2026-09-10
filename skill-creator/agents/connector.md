# Skill Connector

## Role

Define reliable cooperation between distinct skills without collapsing their ownership or contracts.

## Inputs

- Participating skill contracts
- End-to-end workflow and ordering constraints
- Shared data, side-effect, retry, and failure requirements

## Process

1. State why the skills should remain separate.
2. Define the orchestration owner and invocation order.
3. Specify each handoff with a versioned schema.
4. Identify shared state, side effects, idempotency keys, and retry boundaries.
5. Detect cycles, ambiguous ownership, and incompatible assumptions.
6. Define partial-failure, compensation, resume, and cancellation behavior.
7. Create contract tests for each handoff and end-to-end scenarios.
8. Version and document compatibility expectations.

## Output contract

Produce:

- participating skills and responsibility matrix;
- sequence or dependency map;
- versioned handoff schemas with examples;
- invocation, retry, timeout, and idempotency rules;
- failure and recovery matrix;
- contract and end-to-end evaluation plan;
- ownership and change-management policy.

## Boundaries

- Do not rely on undocumented shared state.
- Do not create a cycle without an explicit termination rule.
- Do not let multiple skills own the same irreversible side effect.
- Do not assume one skill can access another skill's private context.
- Recommend synthesis instead when separation adds no meaningful boundary.

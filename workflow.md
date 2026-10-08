# Codebase Workflow

This template uses the **README-as-Blueprint** architecture for software development projects.

## Core 5 Files

1. **README.md** - Living blueprint (executive summary, architecture overview, implementation checklist)
2. **product_requirements.md** - WHAT & WHY (goals, features, acceptance criteria)
3. **architecture.md** - HOW (system design, patterns, technical decisions)
4. **action_log.md** - WHO/WHEN/STATUS (task claims, completions, history)
5. **CLAUDE.md** - Project-specific conventions (Template: codebase)

## Workflow Stages

### Stage 1: Requirements Gathering
- Define problem statement in [product_requirements.md](product_requirements.md)
- Specify functional requirements and acceptance criteria
- Identify constraints and dependencies
- Get stakeholder approval

### Stage 2: Architecture Design
- Create system design in [architecture.md](architecture.md)
- Define module structure and interfaces
- Establish design patterns and conventions
- Update project conventions in [CLAUDE.md](CLAUDE.md)

### Stage 3: Blueprint Creation
- Manager Agent creates implementation blueprint in [README.md](README.md)
- Tasks aligned with requirements and architecture
- Organized by dependencies
- Checkboxed items ready for implementation

### Stage 4: Implementation
- Code Agent implements tasks from README blueprint
- Checks [action_log.md](action_log.md) for WIP first
- Claims task, implements following architecture patterns
- Updates README (removes checkbox, adds documentation)
- Marks complete in action_log

### Stage 5: Testing & Validation
- Test scripts in `claude_test_files/`
- Verify acceptance criteria from requirements
- Performance validation
- Documentation updates

### Stage 6: Completion
- All checkboxes removed from README
- README serves as living documentation
- action_log shows full history
- Code follows architecture patterns

## Commands

- `/manager` - Create implementation blueprints from requirements
- `/code-agent` - Implement features and fix bugs
- `/read` - Quick code lookups without consuming context

## Key Principles

- **Work ONLY in core 5 files** - No planning docs, status reports, etc.
- **Resume interrupted work** - Always check action_log for WIP first
- **Transform blueprints** - README evolves from spec to documentation
- **Follow architecture** - Code Agent respects patterns from architecture.md
- **Test externally** - All test scripts in claude_test_files/

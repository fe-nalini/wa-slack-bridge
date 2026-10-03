"""Recorded deadline comparisons, not inferred business-day calculations or blame."""
from datetime import date, datetime

STEP_NAMES = {'0': 'forms de handoff', '7': 'forms do Club'}

def classify(planned, completed, as_of, original_planned=None):
    # Date-only records expire after that local calendar date; no timezone guess.
    def parse(value):
        if not isinstance(value, str):
            raise ValueError('invalid_date')
        return date.fromisoformat(value)
    try:
        today = parse(as_of)
        due = parse(original_planned or planned)
        done = parse(completed) if completed else None
    except (ValueError, TypeError):
        return {'status': 'insufficient_data', 'out_of_sla': None}
    if done and done > today:
        return {'status': 'inconsistent_future_completion', 'out_of_sla': None}
    if done:
        late = done > due
        return {'status': 'completed_late' if late else 'completed_on_time', 'out_of_sla': late,
                'effective_deadline': due.isoformat(), 'completed': done.isoformat()}
    late = today > due
    return {'status': 'overdue' if late else 'pending_on_time', 'out_of_sla': late,
            'effective_deadline': due.isoformat(), 'completed': None}

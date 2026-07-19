/**
 * Button preview — the `.k-btn` hierarchy as the product uses it:
 * ONE primary per view, quiet for everything else, danger only for
 * destructive actions, `sm` for compact table/card rows.
 */
import * as React from "react";
import { Button } from "@earnings-summary/design-system";

export function Variants() {
  return (
    <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
      <Button variant="primary">Run pipeline</Button>
      <Button variant="quiet">Refresh data</Button>
      <Button variant="danger">Dismiss alert</Button>
      <Button>Save grading</Button>
    </div>
  );
}

export function SmallRow() {
  return (
    <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
      <Button variant="quiet" size="sm">
        review
      </Button>
      <Button variant="quiet" size="sm">
        approve
      </Button>
      <Button variant="danger" size="sm">
        dismiss
      </Button>
    </div>
  );
}

export function Disabled() {
  return (
    <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
      <Button variant="primary" disabled>
        Run pipeline
      </Button>
      <Button variant="quiet" disabled>
        Refresh data
      </Button>
    </div>
  );
}

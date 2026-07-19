/**
 * Toolbar preview — "the one operating band a panel gets" (design_language
 * §6.1): title left, filter chips + actions on the SAME flex row. Composed
 * with the kit's own Chip and Button, exactly as the dashboard's panels do.
 */
import * as React from "react";
import { Button, Chip, Toolbar } from "@earnings-summary/design-system";

export function PanelBand() {
  return (
    <Toolbar
      title="Inbox"
      filters={
        <>
          <Chip interactive on>
            All 14
          </Chip>
          <Chip interactive>Earnings 3</Chip>
          <Chip interactive>Thesis changes 11</Chip>
        </>
      }
      actions={
        <Button variant="quiet" size="sm">
          full feed
        </Button>
      }
    />
  );
}

export function TitleOnly() {
  return <Toolbar title="Portfolio (11)" />;
}

export function SuppressedTitle() {
  return (
    <Toolbar
      title="Decisions"
      suppressTitle
      filters={
        <>
          <Chip interactive on>
            Pending
          </Chip>
          <Chip interactive>Graded</Chip>
        </>
      }
      actions={<Button variant="primary" size="sm">Record decision</Button>}
    />
  );
}

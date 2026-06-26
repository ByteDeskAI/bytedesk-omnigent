import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "./accordion";

describe("AccordionContent", () => {
  it("does not pin the body wrapper to Radix's initially measured height", () => {
    render(
      <Accordion type="multiple" defaultValue={["employees"]}>
        <AccordionItem value="employees">
          <AccordionTrigger>Employee</AccordionTrigger>
          <AccordionContent>
            <button type="button">Maya Chen</button>
          </AccordionContent>
        </AccordionItem>
      </Accordion>,
    );

    const row = screen.getByRole("button", { name: "Maya Chen" });
    const body = row.parentElement;

    expect(body).not.toHaveClass("h-(--radix-accordion-content-height)");
  });
});

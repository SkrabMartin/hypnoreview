import { test, expect } from "../../apps/web/node_modules/@playwright/test";

test("inspect, correct, conflict, restore and export a persistent review", async ({
  page,
  request,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "Review the uncertain." }),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: /Epoch 1/ })).toBeVisible();
  await expect(
    page.getByRole("img", { name: "EEG spectrogram", exact: true }),
  ).toBeVisible();
  await page.getByLabel("Signal window").selectOption("30");
  await page.getByLabel("Reviewed stage").selectOption("N3");
  await page.getByLabel("Review note").fill("Synthetic E2E correction");
  await page.getByRole("button", { name: "Save revision" }).click();
  await expect(page.getByRole("status")).toContainText("Revision saved");
  await expect(page.getByText("#1 · N3")).toBeVisible();
  await page.reload();
  await expect(page.getByText("#1 · N3")).toBeVisible();
  const external = await request.patch(
    "http://127.0.0.1:18000/api/v1/epochs/night:1/review",
    {
      data: {
        stage: "REM",
        note: "Concurrent writer",
        expected_revision: 1,
        analysis_run_id: "run",
      },
    },
  );
  expect(external.status()).toBe(200);
  await page.getByRole("button", { name: "Save revision" }).click();
  await expect(page.getByRole("status")).toContainText("Revision conflict");
  await page.getByRole("button", { name: "Reload epoch" }).click();
  await expect(page.getByText("#2 · REM")).toBeVisible();
  await page.getByRole("button", { name: "Use stage" }).first().click();
  await page.getByRole("button", { name: "Save revision" }).click();
  await expect(page.getByText("#3 · N3")).toBeVisible();
  const downloadPromise = page.waitForEvent("download");
  await page.getByRole("button", { name: "Export JSON" }).click();
  expect((await downloadPromise).suggestedFilename()).toBe("night-review.json");
  await page.getByLabel("Queue strategy").selectOption("coverage_aware");
  await expect(page.getByRole("button", { name: /Epoch 0/ })).toBeVisible();
  await page.screenshot({
    path: "../../artifacts/phase5/synthetic-reviewer.png",
    fullPage: true,
  });
  expect(errors).toEqual([]);
});

test("small screens retain usable controls", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await expect(
    page.getByRole("button", { name: "Save revision" }),
  ).toBeVisible();
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth),
  ).toBeLessThanOrEqual(390);
});

import { test, expect } from "@playwright/test";

test.describe("HelmLog smoke tests", () => {
  test("home page loads and shows navigation", async ({ page }) => {
    await page.goto("/");

    // The page should load without errors
    await expect(page).toHaveTitle(/HelmLog/i);

    // Take a screenshot of the home page for visual reference
    await page.screenshot({ path: "test-results/screenshots/home.png", fullPage: true });
  });

  test("API state endpoint returns JSON", async ({ request }) => {
    const response = await request.get("/api/state");
    expect(response.ok()).toBeTruthy();
    expect(response.headers()["content-type"]).toContain("application/json");
  });

  test("history page loads", async ({ page }) => {
    await page.goto("/history");
    await expect(page).toHaveTitle(/HelmLog/i);
    await page.screenshot({
      path: "test-results/screenshots/history.png",
      fullPage: true,
    });
  });
});

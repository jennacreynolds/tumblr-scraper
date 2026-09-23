const NATIVE_HOST = "tumblr_scraper_firefox";
// Firefox exposes the MV3 name as `action`; older Firefox builds expose the
// same toolbar action as `browserAction`. Keep the adapter compatible at the
// browser boundary; the application remains unchanged.
const toolbarAction = browser.action || browser.browserAction;

if (!toolbarAction) {
  throw new Error("Tumblr Scraper: Firefox toolbar action API is unavailable");
}

async function focusOrOpen(url) {
  const tabs = await browser.tabs.query({});
  const matches = tabs.filter((tab) => tab.url === url);
  if (matches.length) {
    const tab = matches[0];
    await browser.tabs.update(tab.id, {active: true});
    if (tab.windowId !== undefined) {
      await browser.windows.update(tab.windowId, {focused: true});
    }
    return;
  }
  await browser.tabs.create({url, active: true});
}

toolbarAction.onClicked.addListener(async () => {
  let port;
  try {
    port = browser.runtime.connectNative(NATIVE_HOST);
    const response = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("native host timed out")), 45000);
      port.onMessage.addListener((message) => {
        clearTimeout(timer);
        resolve(message);
      });
      port.onDisconnect.addListener(() => {
        clearTimeout(timer);
        reject(new Error(browser.runtime.lastError?.message || "native host disconnected"));
      });
      port.postMessage({action: "open"});
    });
    if (!response || !response.ok || typeof response.url !== "string") {
      throw new Error(response?.error || "native host returned no application URL");
    }
    await focusOrOpen(response.url);
  } catch (error) {
    console.error("Tumblr Scraper could not open:", error);
    await toolbarAction.setBadgeText({text: "!"});
    await toolbarAction.setBadgeBackgroundColor({color: "#c33"});
  } finally {
    if (port) port.disconnect();
  }
});

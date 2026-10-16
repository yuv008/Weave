const vscode = require("vscode");

const VIEW_TYPE = "wisp.companion";

function closeCompanionTabs() {
  const tabs = [];
  for (const group of vscode.window.tabGroups.all) {
    for (const tab of group.tabs) {
      const input = tab.input;
      if (input instanceof vscode.TabInputWebview && input.viewType === VIEW_TYPE) tabs.push(tab);
    }
  }
  if (tabs.length) vscode.window.tabGroups.close(tabs);
}

function activate(context) {
  context.subscriptions.push(vscode.window.registerWebviewPanelSerializer(VIEW_TYPE, {
    deserializeWebviewPanel(panel) {
      panel.dispose();
    },
  }));
  closeCompanionTabs();
  setTimeout(closeCompanionTabs, 600);
}

function deactivate() {}

module.exports = { activate, deactivate };

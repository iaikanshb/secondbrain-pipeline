/*
Second Brain Semantic Search -- a thin Obsidian-native front end for
search.py. Deliberately shells out to the existing Python script (via
Node's child_process, available because this plugin is desktop-only)
rather than reimplementing embedding calls or SQLite reading in
JavaScript: search.py, embeddings.py, and the index format are the single
source of truth, maintained in one place and one language. This plugin
adds only UI.

No build step / bundler / dependencies on purpose -- plain CommonJS,
loaded by Obsidian as-is. Copy this folder into <vault>/.obsidian/plugins/
and enable it in Settings -> Community plugins -> Installed plugins (this
isn't published to the official community plugin directory, so it won't
appear in the in-app browser -- manual install only).
*/
const { Plugin, SuggestModal, PluginSettingTab, Setting, Notice } = require("obsidian");
const { execFile } = require("child_process");

const DEFAULT_SETTINGS = {
  pythonPath: "python3",
  scriptPath: "",
  minChars: 3,
  debounceMs: 300,
  resultLimit: 10,
};

class SemanticSearchModal extends SuggestModal {
  constructor(app, plugin) {
    super(app);
    this.plugin = plugin;
    this._debounceTimer = null;
    this.setPlaceholder("Search by meaning, not just keywords...");
    this.setInstructions([
      { command: "↑↓", purpose: "navigate" },
      { command: "↵", purpose: "open note" },
      { command: "esc", purpose: "dismiss" },
    ]);
  }

  getSuggestions(query) {
    const { minChars, debounceMs } = this.plugin.settings;
    if (!query || query.trim().length < minChars) return [];

    return new Promise((resolve) => {
      if (this._debounceTimer) clearTimeout(this._debounceTimer);
      this._debounceTimer = setTimeout(() => {
        this._runSearch(query.trim()).then(resolve);
      }, debounceMs);
    });
  }

  _runSearch(query) {
    const { pythonPath, scriptPath, resultLimit } = this.plugin.settings;
    return new Promise((resolve) => {
      execFile(pythonPath, [scriptPath, query], { timeout: 20000 }, (err, stdout, stderr) => {
        if (err) {
          resolve([{ error: (stderr || err.message || "search failed").trim() }]);
          return;
        }
        const results = stdout
          .trim()
          .split("\n")
          .filter(Boolean)
          .map((line) => {
            const m = line.match(/^([\d.]+)\s+(.+)$/);
            return m ? { score: m[1], title: m[2] } : null;
          })
          .filter(Boolean)
          .slice(0, resultLimit);

        if (!results.length) {
          resolve([{ error: stdout.trim() || "no matches" }]);
          return;
        }
        resolve(results);
      });
    });
  }

  renderSuggestion(item, el) {
    if (item.error) {
      el.createEl("div", { text: item.error, cls: "semantic-search-error" });
      return;
    }
    el.createEl("div", { text: item.title, cls: "semantic-search-title" });
    el.createEl("small", { text: `similarity ${item.score}`, cls: "semantic-search-score" });
  }

  onChooseSuggestion(item) {
    if (item.error || !item.title) return;
    const file = this.app.metadataCache.getFirstLinkpathDest(item.title, "");
    if (file) {
      this.app.workspace.getLeaf(false).openFile(file);
    } else {
      new Notice(`Note not found in vault: ${item.title}`);
    }
  }
}

class SemanticSearchSettingTab extends PluginSettingTab {
  constructor(app, plugin) {
    super(app, plugin);
    this.plugin = plugin;
  }

  display() {
    const { containerEl } = this;
    containerEl.empty();
    containerEl.createEl("h2", { text: "Second Brain Semantic Search" });
    containerEl.createEl("p", {
      text: "Complements Obsidian's built-in search / Omnisearch -- use those when you "
        + "remember roughly the words used, this when you only remember the idea.",
    });

    new Setting(containerEl)
      .setName("Python executable")
      .setDesc('Path to python3 (the pipeline\'s own .venv works too), or just "python3" if it\'s on PATH')
      .addText((text) =>
        text.setValue(this.plugin.settings.pythonPath).onChange(async (value) => {
          this.plugin.settings.pythonPath = value.trim() || DEFAULT_SETTINGS.pythonPath;
          await this.plugin.saveSettings();
        })
      );

    new Setting(containerEl)
      .setName("search.py path")
      .setDesc("Full path to secondbrain-pipeline/search.py on this machine")
      .addText((text) =>
        text
          .setPlaceholder("/home/you/secondbrain-pipeline/search.py")
          .setValue(this.plugin.settings.scriptPath)
          .onChange(async (value) => {
            this.plugin.settings.scriptPath = value.trim();
            await this.plugin.saveSettings();
          })
      );

    new Setting(containerEl)
      .setName("Minimum query length")
      .setDesc("Don't search until the query is at least this many characters")
      .addText((text) =>
        text.setValue(String(this.plugin.settings.minChars)).onChange(async (value) => {
          const n = parseInt(value, 10);
          this.plugin.settings.minChars = Number.isFinite(n) && n > 0 ? n : DEFAULT_SETTINGS.minChars;
          await this.plugin.saveSettings();
        })
      );
  }
}

module.exports = class SecondBrainSearchPlugin extends Plugin {
  async onload() {
    await this.loadSettings();

    this.addCommand({
      id: "semantic-search",
      name: "Semantic search across vault",
      callback: () => {
        if (!this.settings.scriptPath) {
          new Notice("Set the path to search.py in this plugin's settings first.");
          return;
        }
        new SemanticSearchModal(this.app, this).open();
      },
    });

    this.addSettingTab(new SemanticSearchSettingTab(this.app, this));
  }

  async loadSettings() {
    this.settings = Object.assign({}, DEFAULT_SETTINGS, await this.loadData());
  }

  async saveSettings() {
    await this.saveData(this.settings);
  }
};

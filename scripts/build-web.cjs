const fs = require("node:fs");
const path = require("node:path");
const esbuild = require("esbuild");

async function build() {
  const root = path.resolve(__dirname, "..");
  const result = await esbuild.build({
    stdin: {
      contents: 'import * as msal from "@azure/msal-browser"; window.msal = msal;',
      resolveDir: root,
      sourcefile: "microsoft-sign-in.js",
    },
    bundle: true,
    minify: true,
    legalComments: "inline",
    platform: "browser",
    target: "es2022",
    write: false,
  });
  const template = fs.readFileSync(path.join(root, "mcp", "web", "template.html"), "utf8");
  if (template.split("<!--MSAL_BUNDLE-->").length !== 2) {
    throw new Error("Expected exactly one MSAL bundle placeholder");
  }
  const bundle = result.outputFiles[0].text.replace(/<\/script/gi, "<\\/script");
  fs.writeFileSync(
    path.join(root, "mcp", "web", "index.html"),
    template.replace("<!--MSAL_BUNDLE-->", () => `<script>${bundle}</script>`),
  );
  console.log("Built self-contained mcp\\web\\index.html.");
}
build().catch(error => {
  console.error(error);
  process.exitCode = 1;
});

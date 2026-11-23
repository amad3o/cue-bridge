import { render } from "ink";
import { App } from "./App";

const app = render(<App />, { exitOnCtrlC: false });
await app.waitUntilExit();
process.exit(0);

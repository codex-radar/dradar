// Only evaluate the published argument parsers: never boot a DSH profile.
const fs = require('node:fs');
const vm = require('node:vm');
const { Command, CommanderError } = require('commander');
const source = fs.readFileSync(`${__dirname}/bin.js`, 'utf8');
const startup = fs.readFileSync(`${__dirname}/startup.js`, 'utf8');
let invocation;
const context = {
  Command, CommanderError, process,
  parseCmdline: (_ctx, program) => program.parse(invocation.args, { from: 'user' }),
};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('const collect ='), source.indexOf('//#region lib/types/bin.js')), context);
vm.runInContext(startup.slice(startup.indexOf('const HEADLESS_STARTUP_SERVICE'), startup.indexOf('//#endregion')), context);
invocation = context.parseDshArgs(process.argv.slice(2), '0.1.2-rc.1');
context.apply({ provide: (service, value) => {
  process.stdout.write(JSON.stringify({ invocation, service, ...value }) + '\n');
} });
// Simulate a post-parse failure without loading any provider or writing sidecars.
if (process.env.DRADAR_PARSER_TEST_EXIT) {
  process.stderr.write('synthetic DSH execution failure\n');
  process.exit(Number(process.env.DRADAR_PARSER_TEST_EXIT));
}

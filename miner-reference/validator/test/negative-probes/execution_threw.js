// @expectedRule EXECUTION_THREW
// generate() throws at runtime.
export default function generate(THREE) {
  throw new Error('boom');
}

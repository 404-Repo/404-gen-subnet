/** Browser deadlines shared by the renderer and its Puppeteer connection. */
export function renderTimeoutMs(isFinal = false) {
  const variable = isFinal ? 'FINAL_RENDER_TIMEOUT_MS' : 'RENDER_TIMEOUT_MS';
  return parseInt(process.env[variable] || (isFinal ? '150000' : '30000'), 10);
}

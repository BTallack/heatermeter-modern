// What the pit is doing, in words. Fork firmware reports a controller mode
// (pid_mode / pid_mode_label); stock firmware does not, so there the state is
// derived from the setpoint, the pit reading, and whether Pit Guard has seen
// the pit reach temperature since the setpoint last changed.

// Degrees either side of the setpoint that count as "holding".
export const holdBand = (unit) => (unit === 'C' ? 6 : 10);

export function pitState(status, guard, unit = 'F') {
  if (!status || status.pit === undefined) return { key: 'connecting', label: 'Connecting…', active: false };
  if (status.lid_countdown > 0) return { key: 'lid', label: 'Lid open', active: true };
  if (status.pid_mode_label) {
    return { key: 'mode', label: status.pid_mode_label, active: [0, 1, 2, 3].includes(status.pid_mode) };
  }
  const sp = typeof status.set_point === 'number' ? status.set_point : null;
  if (sp == null || sp === 0) return { key: 'off', label: 'Off', active: false };
  if (sp < 0) return { key: 'manual', label: 'Manual fan', active: true };
  const pit = status.pit;
  if (pit == null) return { key: 'noprobe', label: 'No pit probe', active: true };
  const band = holdBand(unit);
  if (pit > sp + band) return { key: 'above', label: 'Above set', active: true };
  if (pit >= sp - band) return { key: 'holding', label: 'Holding', active: true };
  return guard?.armed
    ? { key: 'below', label: 'Below set', active: true }
    : { key: 'climbing', label: 'Climbing', active: true };
}

// A cook is running when the pit has a setpoint (positive = controlled,
// negative = manual fan) or the firmware says the controller is working.
export function cookActive(status) {
  if (!status) return false;
  if ([0, 1, 2, 3].includes(status.pid_mode)) return true;
  return typeof status.set_point === 'number' && status.set_point !== 0;
}

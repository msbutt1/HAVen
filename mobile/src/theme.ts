/** The dashboard's livops colour tokens (dashboard/css/style.css), light and dark, as in the native apps. */
import { useColorScheme } from 'react-native';

const light = {
  background: '#FFFFFF', card: '#FAFBFC', section: '#F3F4F7', foreground: '#0B0F19', muted: '#666D7C',
  border: '#DFE2E8', primary: '#006AE6', onPrimary: '#FBFCFD', success: '#008140', onSuccess: '#FBFDFC',
  warning: '#E4960E', destructive: '#E30008', sim: '#A041B5', sim2: '#8B329E',
  activation: '#006AE6', vibration: '#CC6600', score: '#008774',
  // Summary charts (the dashboard's --ov-* slots)
  ovBlue: '#006AE6', ovBlue2: '#82B0F3', ovOrange: '#CC6600', ovTeal: '#008774', ovGreen: '#008140',
  ovViolet: '#A041B5', ovNeutral: '#A1A5AC', ovTrack: '#E5E8ED',
};
const dark: typeof light = {
  background: '#0C0F15', card: '#151921', section: '#10141A', foreground: '#EEF0F4', muted: '#99A0AF',
  border: '#2A2E37', primary: '#4495FF', onPrimary: '#060D1A', success: '#3EB268', onSuccess: '#041107',
  warning: '#EDA839', destructive: '#FF6468', sim: '#AE55C1', sim2: '#9043A1',
  activation: '#4495FF', vibration: '#F59740', score: '#39C3AE',
  ovBlue: '#3082F6', ovBlue2: '#2C5A9D', ovOrange: '#D4771A', ovTeal: '#00A38F', ovGreen: '#2EA55C',
  ovViolet: '#AE55C1', ovNeutral: '#60636A', ovTrack: '#2A2E35',
};

export type Theme = typeof light;

export function useTheme(): Theme {
  return useColorScheme() === 'dark' ? dark : light;
}

export const SOURCE_COLOR = (t: Theme) => ({ live: t.ovGreen, replay: t.ovBlue, simulated: t.ovViolet });

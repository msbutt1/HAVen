import { Stack } from 'expo-router/stack';
import { StatusBar } from 'expo-status-bar';
import { Text, View } from 'react-native';
import { SafeAreaProvider, useSafeAreaInsets } from 'react-native-safe-area-context';

import { useStore } from '../state/store';
import { useTheme } from '../theme';

function Toast() {
  const t = useTheme();
  const s = useStore();
  const insets = useSafeAreaInsets();
  if (!s.toast) return null;
  return (
    <View pointerEvents="none" style={{ position: 'absolute', left: 20, right: 20, bottom: insets.bottom + 90, alignItems: 'center' }}>
      <View accessibilityLiveRegion="polite" style={{ backgroundColor: t.card, borderColor: t.border, borderWidth: 1, borderRadius: 12,
        paddingVertical: 12, paddingHorizontal: 16, shadowColor: '#000', shadowOpacity: 0.25, shadowRadius: 10, elevation: 6 }}>
        <Text style={{ color: t.foreground, fontSize: 16, fontWeight: '600', textAlign: 'center' }}>{s.toast}</Text>
      </View>
    </View>
  );
}

export default function RootLayout() {
  const t = useTheme();
  return (
    <SafeAreaProvider>
      <StatusBar style="auto" />
      <Stack screenOptions={{ headerShown: false, contentStyle: { backgroundColor: t.background } }}>
        <Stack.Screen name="index" />
        <Stack.Screen name="(tabs)" />
        <Stack.Screen name="scan" options={{ presentation: 'fullScreenModal' }} />
      </Stack>
      <Toast />
    </SafeAreaProvider>
  );
}

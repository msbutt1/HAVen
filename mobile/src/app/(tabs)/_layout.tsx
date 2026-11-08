/** The five main screens, as a bottom menu with an icon over a word. */
import { Ionicons } from '@expo/vector-icons';
import { router } from 'expo-router';
import { Tabs } from 'expo-router/js-tabs';
import { type ColorValue, Pressable, Text } from 'react-native';

import { useTheme } from '../../theme';

function LaptopButton() {
  const t = useTheme();
  return (
    <Pressable accessibilityRole="button" accessibilityLabel="Change laptop" onPress={() => router.push({ pathname: '/', params: { change: '1' } })}
      style={{ flexDirection: 'row', alignItems: 'center', gap: 6, paddingHorizontal: 16, minHeight: 44 }}>
      <Ionicons name="laptop-outline" size={22} color={t.primary} />
      <Text style={{ color: t.primary, fontSize: 17, fontWeight: '600' }}>Laptop</Text>
    </Pressable>
  );
}

export default function TabsLayout() {
  const t = useTheme();
  const icon = (name: keyof typeof Ionicons.glyphMap) =>
    ({ color, size }: { color: ColorValue; size: number }) => <Ionicons name={name} size={size + 2} color={color as string} />;
  return (
    <Tabs screenOptions={{
      headerStyle: { backgroundColor: t.background },
      headerTintColor: t.foreground,
      headerTitleStyle: { fontWeight: '700', fontSize: 19 },
      headerShadowVisible: false,
      headerLeft: () => <LaptopButton />,
      tabBarActiveTintColor: t.primary,
      tabBarInactiveTintColor: t.muted,
      tabBarStyle: { backgroundColor: t.background, borderTopColor: t.border, minHeight: 64 },
      tabBarLabelStyle: { fontSize: 12, fontWeight: '600' },
      sceneStyle: { backgroundColor: t.background },
    }}>
      <Tabs.Screen name="live" options={{ title: 'Live', tabBarIcon: icon('pulse') }} />
      <Tabs.Screen name="summary" options={{ title: 'Summary', tabBarIcon: icon('stats-chart') }} />
      <Tabs.Screen name="calibrate" options={{ title: 'Calibrate', tabBarIcon: icon('speedometer') }} />
      <Tabs.Screen name="test" options={{ title: 'Touch test', tabBarIcon: icon('hand-left') }} />
      <Tabs.Screen name="history" options={{ title: 'History', tabBarIcon: icon('time') }} />
    </Tabs>
  );
}

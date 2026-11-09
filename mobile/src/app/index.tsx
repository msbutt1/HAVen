/**
 * The connect screen, for people who may be new to phones. It starts on its own (the laptop used last time); the main
 * way in is scanning the QR code the laptop prints when HAVen starts for phones. Help and a typed address wait
 * behind "Can't connect?"; practice mode needs no laptop.
 */
import { Ionicons } from '@expo/vector-icons';
import { router, useFocusEffect, useLocalSearchParams } from 'expo-router';
import { useCallback, useEffect, useRef, useState } from 'react';
import { ActivityIndicator, KeyboardAvoidingView, Platform, ScrollView, type ScrollViewInstance, Text, TextInput, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';

import { BigButton, Muted, Panel, Title } from '../components/ui';
import { scanned } from '../state/scan';
import { store, useStore } from '../state/store';
import { useTheme } from '../theme';
import { baseURL, basicAuth, label } from '../vg/address.ts';

export default function Connect() {
  const t = useTheme();
  const s = useStore();
  const insets = useSafeAreaInsets();
  const params = useLocalSearchParams<{ change?: string }>();
  const [attempt, setAttempt] = useState<string | null>(null);
  const [forward, setForward] = useState(false);   // go on to the main screens once the connection started here opens
  const [showHelp, setShowHelp] = useState(false);
  const [typed, setTyped] = useState('');
  const [typedError, setTypedError] = useState('');
  const [user, setUser] = useState('');
  const [password, setPassword] = useState('');
  const [scanError, setScanError] = useState('');
  const started = useRef(false);
  const scroller = useRef<ScrollViewInstance>(null);

  const open = useCallback((base: string, u = user, p = password) => {
    setAttempt(base);
    setForward(true);
    scroller.current?.scrollTo({ y: 0, animated: true });   // the result shows at the top
    setScanError('');
    store.connectLaptop(base, basicAuth(u, p), { user: u, password: p });
  }, [user, password]);

  // first open: reopen the laptop used last time (not when the user came here to change laptop)
  useEffect(() => {
    if (started.current || params.change) return;
    started.current = true;
    store.lastLaptop().then((l) => {
      if (l && !store.s.connected) {
        setUser(l.user);
        setPassword(l.password);
        open(l.base, l.user, l.password);
      }
    });
  }, [open, params.change]);

  // a QR code scanned on the scan screen
  useFocusEffect(useCallback(() => {
    const text = scanned.take();
    if (text == null) return;
    const base = baseURL(text);
    if (base) open(base);
    else setScanError('That QR code is not a HAVen laptop. Scan the code the laptop shows when HAVen starts.');
  }, [open]));

  // connected (by something started on this screen): on to the main screens
  useEffect(() => {
    if (forward && s.connected) router.replace('/live');
  }, [forward, s.connected]);

  const connectTyped = () => {
    const base = baseURL(typed);
    if (!base) { setTypedError('Type the laptop’s address, for example 192.168.1.23 or 192.168.1.23:8000.'); return; }
    setTypedError('');
    open(base);
  };

  const opening = attempt != null && !s.connected && !s.problem;
  const failed = attempt != null && !s.connected && !!s.problem;

  return (
    <KeyboardAvoidingView behavior={Platform.OS === 'ios' ? 'padding' : undefined}
      style={{ flex: 1, backgroundColor: t.background, paddingTop: insets.top }}>
      <ScrollView ref={scroller} contentContainerStyle={{ padding: 20, paddingTop: 16, paddingBottom: insets.bottom + 32, gap: 16 }}
        keyboardShouldPersistTaps="handled">
        <View style={{ flexDirection: 'row', alignItems: 'center', gap: 12 }}>
          <View style={{ width: 40, height: 40, borderRadius: 11, backgroundColor: t.primary, alignItems: 'center', justifyContent: 'center' }}>
            <Ionicons name="pulse" size={26} color={t.onPrimary} />
          </View>
          <Text style={{ color: t.foreground, fontSize: 24, fontWeight: '700' }}>HAVen</Text>
        </View>
        <Text accessibilityRole="header" style={{ color: t.foreground, fontSize: 32, fontWeight: '800' }}>Connect to the laptop</Text>
        <Muted size={17}>The laptop talks to the sleeve and keeps the recordings. This phone shows them over Wi-Fi. Phone and laptop must be on the same Wi-Fi.</Muted>

        {params.change && s.connected && !forward && (
          <Panel style={{ gap: 10 }}>
            <Muted size={16}>{s.mode === 'demo' ? 'Practice mode is open now.' : `Connected now to ${label(s.base ?? '')}.`}</Muted>
            <BigButton title="Keep using it" kind="outline" onPress={() => router.back()} />
          </Panel>
        )}

        <Panel style={{ padding: 20, gap: 14 }}>
          {opening && (
            <View accessibilityLiveRegion="polite" style={{ flexDirection: 'row', alignItems: 'center', gap: 12 }}>
              <ActivityIndicator color={t.primary} />
              <Text style={{ color: t.foreground, fontSize: 21, fontWeight: '700', flex: 1 }}>Opening {label(attempt!)}…</Text>
            </View>
          )}
          {failed && (
            <View accessibilityLiveRegion="polite" style={{ gap: 6 }}>
              <Text style={{ color: t.foreground, fontSize: 21, fontWeight: '700' }}>Could not open {label(attempt!)}</Text>
              <Muted size={17}>{s.problem}</Muted>
            </View>
          )}
          {failed && s.needsPassword && (
            <View style={{ gap: 10 }}>
              <Field value={user} onChange={setUser} placeholder="User name" />
              <Field value={password} onChange={setPassword} placeholder="Password" secure />
              <BigButton title="Sign in" onPress={() => open(attempt!)} />
            </View>
          )}
          {!opening && !failed && (
            <View style={{ gap: 6 }}>
              <Text style={{ color: t.foreground, fontSize: 21, fontWeight: '700' }}>Scan the laptop’s QR code</Text>
              <Muted size={17}>When HAVen starts on the laptop for phones, it shows a square QR code. Point this phone’s camera at it.</Muted>
            </View>
          )}
          {!!scanError && <Text style={{ color: t.destructive, fontSize: 16 }}>{scanError}</Text>}
          {!opening && (
            <BigButton title={failed ? 'Scan again' : 'Scan the QR code'} onPress={() => router.push('/scan')}
              icon={<Ionicons name="qr-code" size={24} color={t.onPrimary} />} height={68} />
          )}
          {failed && !s.needsPassword && <BigButton title="Try again" kind="outline" onPress={() => open(attempt!)} />}
        </Panel>

        <BigButton title={showHelp ? 'Hide help' : 'Can’t connect?'} kind="outline" onPress={() => setShowHelp(!showHelp)} />
        {showHelp && (
          <Panel style={{ padding: 20, gap: 12 }}>
            <Title>Getting connected</Title>
            <Text style={{ color: t.foreground, fontSize: 17, lineHeight: 24 }}>
              1. Put the phone and the laptop on the same Wi-Fi.{'\n'}2. On the laptop, start HAVen for phones (the person who set up the laptop can do this):
            </Text>
            <Text selectable style={{ fontFamily: Platform.OS === 'ios' ? 'Menlo' : 'monospace', fontSize: 15, color: t.foreground,
              backgroundColor: t.section, borderRadius: 10, padding: 12 }}>vg-backend serve --device wifi --host 0.0.0.0</Text>
            <Muted size={16}>3. It prints the QR code. If the laptop asks whether Python may accept incoming connections, choose Allow.{'\n\n'}Some networks keep devices apart (eduroam, guest or company Wi-Fi). Then use a phone hotspot or a separate router.</Muted>
            <Title>Or type the laptop’s address</Title>
            <Muted size={16}>Printed under the QR code, for example 192.168.1.23. Add the port if it is not 8000.</Muted>
            <Field value={typed} onChange={(v) => { setTyped(v); setTypedError(''); }} placeholder="192.168.1.23" mono onSubmit={connectTyped} />
            {!!typedError && <Text style={{ color: t.destructive, fontSize: 15 }}>{typedError}</Text>}
            <BigButton title="Connect" onPress={connectTyped} disabled={!typed.trim()} />
          </Panel>
        )}

        <View style={{ borderRadius: 16, padding: 20, gap: 10, borderWidth: 1.5, borderStyle: 'dashed', borderColor: t.sim, backgroundColor: t.card }}>
          <Title>No laptop nearby?</Title>
          <Muted size={16}>Try every screen with made-up data that runs on this phone. Nothing in it is a measurement from a person.</Muted>
          <BigButton title="Practice with made-up data" kind="outline" onPress={() => { setAttempt(null); setForward(true); store.connectDemo(); }} />
        </View>

        <Muted size={13}>Not a medical device. Vibration is unweighted (not ISO 5349); grip effort is relative to each wearer’s calibration, not force.</Muted>
      </ScrollView>
    </KeyboardAvoidingView>
  );
}

function Field({ value, onChange, placeholder, secure, mono, onSubmit }: {
  value: string; onChange: (v: string) => void; placeholder: string; secure?: boolean; mono?: boolean; onSubmit?: () => void;
}) {
  const t = useTheme();
  return (
    <TextInput value={value} onChangeText={onChange} placeholder={placeholder} placeholderTextColor={t.muted}
      secureTextEntry={secure} autoCapitalize="none" autoCorrect={false} keyboardType={mono ? 'url' : 'default'}
      returnKeyType={onSubmit ? 'go' : 'done'} onSubmitEditing={onSubmit} accessibilityLabel={placeholder}
      style={{ minHeight: 56, borderRadius: 12, borderWidth: 1, borderColor: t.border, paddingHorizontal: 14, fontSize: 19,
        color: t.foreground, backgroundColor: t.background, fontFamily: mono ? (Platform.OS === 'ios' ? 'Menlo' : 'monospace') : undefined }} />
  );
}


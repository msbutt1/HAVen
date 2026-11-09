/** Scan the QR code the laptop prints (http://<laptop>:<port>/dashboard/). The camera only reads QR codes. */
import { CameraView, useCameraPermissions } from 'expo-camera';
import { router } from 'expo-router';
import { useState } from 'react';
import { Text, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';

import { BigButton, Muted } from '../components/ui';
import { scanned } from '../state/scan';
import { useTheme } from '../theme';
import { baseURL } from '../vg/address.ts';

export default function Scan() {
  const t = useTheme();
  const insets = useSafeAreaInsets();
  const [permission, requestPermission] = useCameraPermissions();
  const [hint, setHint] = useState('');
  const [done, setDone] = useState(false);

  if (!permission) return <View style={{ flex: 1, backgroundColor: t.background }} />;

  if (!permission.granted) {
    return (
      <View style={{ flex: 1, backgroundColor: t.background, padding: 24, paddingTop: insets.top + 40, gap: 16 }}>
        <Text style={{ color: t.foreground, fontSize: 28, fontWeight: '800' }}>Use the camera</Text>
        <Muted size={18}>HAVen uses the camera only to read the QR code on the laptop. Nothing is recorded.</Muted>
        {permission.canAskAgain
          ? <BigButton title="Allow the camera" onPress={requestPermission} height={68} />
          : <Muted size={17}>The camera is turned off for HAVen. Turn it on in the phone’s Settings, or go back and type the laptop’s address.</Muted>}
        <BigButton title="Go back" kind="outline" onPress={() => router.back()} />
      </View>
    );
  }

  return (
    <View style={{ flex: 1, backgroundColor: '#000' }}>
      <CameraView style={{ flex: 1 }} facing="back" barcodeScannerSettings={{ barcodeTypes: ['qr'] }}
        onBarcodeScanned={done ? undefined : ({ data }) => {
          if (!baseURL(data)) { setHint('That QR code is not a HAVen laptop.'); return; }
          setDone(true);
          scanned.set(data);
          router.back();
        }} />
      <View pointerEvents="box-none" style={{ position: 'absolute', left: 0, right: 0, top: insets.top + 24, alignItems: 'center', paddingHorizontal: 24 }}>
        <Text style={{ color: '#fff', fontSize: 22, fontWeight: '700', textAlign: 'center', textShadowColor: '#000', textShadowRadius: 6 }}>
          Point the camera at the QR code on the laptop
        </Text>
        {!!hint && <Text style={{ color: '#ffd27a', fontSize: 17, marginTop: 8, textAlign: 'center' }}>{hint}</Text>}
      </View>
      <View pointerEvents="none" style={{ position: 'absolute', alignSelf: 'center', top: '30%', width: 250, height: 250,
        borderColor: '#fff', borderWidth: 3, borderRadius: 24 }} />
      <View style={{ position: 'absolute', left: 24, right: 24, bottom: insets.bottom + 24 }}>
        <BigButton title="Cancel" kind="outline" onPress={() => router.back()} height={64} />
      </View>
    </View>
  );
}

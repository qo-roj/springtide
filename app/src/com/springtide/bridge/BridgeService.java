package com.springtide.bridge;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.bluetooth.BluetoothAdapter;
import android.bluetooth.BluetoothDevice;
import android.bluetooth.BluetoothGatt;
import android.bluetooth.BluetoothGattCallback;
import android.bluetooth.BluetoothGattCharacteristic;
import android.bluetooth.BluetoothGattDescriptor;
import android.bluetooth.BluetoothGattService;
import android.bluetooth.BluetoothManager;
import android.bluetooth.BluetoothProfile;
import android.bluetooth.le.BluetoothLeScanner;
import android.bluetooth.le.ScanCallback;
import android.bluetooth.le.ScanResult;
import android.content.Context;
import android.content.Intent;
import android.os.IBinder;
import android.util.Log;

import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.net.SocketTimeoutException;
import java.util.UUID;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

/**
 * OBD Bridge: BLE GATT (NORAUTO BT36044 ELM327) <-> TCP 127.0.0.1:35000.
 * Started via (any of): launcher activity tap, BOOT_COMPLETED receiver,
 * or `am start-foreground-service --user 0 -n com.springtide.bridge/.BridgeService`.
 * Logs under tag OBDBridge. Dumb pipe: no ELM logic here.
 *
 * Review 2026-11-02 (dual-model): a failed GATT setup (no write
 * characteristic, or a disconnect before services were ready) must NOT
 * release the cycle() thread into serverLoop() on a dead pipe — the
 * readyOk flag only turns true on a fully usable GATT, and cycle() fails
 * fast otherwise so the main loop rescans. Per-client sockets are closed
 * in a finally block so an exception can't leak them.
 */
@SuppressWarnings("deprecation")
public class BridgeService extends Service {
    static final String TAG = "OBDBridge";
    static final int PORT = 35000;
    static final String NAME_FILTER = "BT36044";
    // 2026-11-04: the dongle came back from a power cycle advertising WITHOUT its
    // name — invisible to a name-filtered scan while saved-MAC clients (Car
    // Scanner) still connected fine. Match the known address too. Nameless
    // scan results report getName() == null; the address is always present.
    static final String DONGLE_MAC = "28:82:07:0B:A3:22";
    static final UUID CCCD = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb");

    BluetoothAdapter adapter;
    BluetoothGatt gatt;
    BluetoothGattCharacteristic writeChar, notifyChar;
    volatile boolean bleReady = false;
    volatile int mtu = 23;
    volatile Socket client;
    volatile OutputStream clientOut;
    CountDownLatch scanLatch;
    volatile BluetoothDevice found;
    Thread mainThread;

    @Override
    public void onCreate() {
        super.onCreate();
        NotificationManager nm = (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
        NotificationChannel ch = new NotificationChannel("obdbridge", "OBD Bridge", NotificationManager.IMPORTANCE_LOW);
        nm.createNotificationChannel(ch);
        Notification n = new Notification.Builder(this, "obdbridge")
                .setContentTitle("OBD Bridge")
                .setContentText("BLE->TCP bridge running")
                .setSmallIcon(android.R.drawable.stat_sys_data_bluetooth)
                .setOngoing(true)
                .build();
        startForeground(42, n);
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (mainThread == null || !mainThread.isAlive()) {
            mainThread = new Thread(this::mainLoop, "bridge-main");
            mainThread.start();
        }
        return START_STICKY;
    }

    @Override
    public IBinder onBind(Intent intent) { return null; }

    static void sleep(long ms) { try { Thread.sleep(ms); } catch (InterruptedException e) { Thread.currentThread().interrupt(); } }
    static void log(String s) { Log.i(TAG, s); }

    void mainLoop() {
        log("bridge main loop start");
        while (true) {
            try { cycle(); }
            catch (Exception e) { log("cycle error: " + e); }
            teardown();
            sleep(3000);
        }
    }

    void cycle() throws Exception {
        adapter = ((BluetoothManager) getSystemService(Context.BLUETOOTH_SERVICE)).getAdapter();
        if (adapter == null) { log("no BT adapter"); sleep(30000); return; }
        int waited = 0;
        while (!adapter.isEnabled()) {
            if (waited == 0) {
                // 2026-11-11: root is gone (OTA wiped Magisk) — `su -c 'cmd
                // bluetooth_manager enable'` is no longer an option. BLUETOOTH_CONNECT
                // is pm-granted, so try enabling the radio ourselves; if the platform
                // refuses, the wait loop keeps watching (radio may come on manually).
                log("BT radio OFF - attempting adapter.enable()");
                try { adapter.enable(); }
                catch (SecurityException e) { log("enable() denied: " + e); }
            }
            sleep(5000); waited += 5;
            if (waited >= 300) return;
        }
        found = null;
        scanLatch = new CountDownLatch(1);
        BluetoothLeScanner scanner = adapter.getBluetoothLeScanner();
        if (scanner == null) { log("no BLE scanner"); return; }
        log("scanning for " + NAME_FILTER + " ...");
        scanner.startScan(scanCb);
        boolean got = scanLatch.await(25, TimeUnit.SECONDS);
        try { scanner.stopScan(scanCb); } catch (Exception ignored) {}
        boolean background;
        if (!got || found == null) {
            // 2026-11-04: the dongle can stop advertising entirely (came back
            // nameless after a power cycle, then fully silent). A background
            // direct connect to the known MAC parks at the controller and
            // completes whenever the dongle transmits anything connectable —
            // the same way Car Scanner reaches it via its saved adapter entry.
            log("dongle not visible — background connect to " + DONGLE_MAC);
            try { found = adapter.getRemoteDevice(DONGLE_MAC); }
            catch (IllegalArgumentException e) { log("bad MAC: " + DONGLE_MAC); return; }
            background = true;
        } else {
            log("dongle: " + found.getName() + " " + found.getAddress());
            background = false;
        }

        CountDownLatch ready = new CountDownLatch(1);
        gattCb.readyOk = false;
        gattCb.readyLatch = ready;
        gatt = found.connectGatt(this, background, gattCb);  // autoConnect when no scan hit
        if (gatt == null) { log("connectGatt failed"); return; }
        if (!ready.await(background ? 300 : 25, TimeUnit.SECONDS)) { log("GATT setup timeout"); return; }
        if (!gattCb.readyOk) { log("GATT setup failed (no chars or early disconnect)"); return; }
        bleReady = true;
        log("BLE pipe ready, mtu=" + mtu + " port=" + PORT);
        serverLoop();
    }

    final ScanCallback scanCb = new ScanCallback() {
        @Override public void onScanResult(int callbackType, ScanResult result) {
            BluetoothDevice d = result.getDevice();
            if (found != null) return;
            String n = null;
            try { n = d.getName(); } catch (SecurityException ignored) {}
            if ((n != null && n.toUpperCase().contains(NAME_FILTER))
                    || DONGLE_MAC.equals(d.getAddress())) {
                found = d;
                scanLatch.countDown();
            }
        }
        @Override public void onScanFailed(int errorCode) {
            log("scan failed code=" + errorCode);
            scanLatch.countDown();
        }
    };

    final GattCb gattCb = new GattCb();

    class GattCb extends BluetoothGattCallback {
        CountDownLatch readyLatch;
        volatile boolean readyOk = false;   // true ONLY on a fully usable GATT

        @Override public void onConnectionStateChange(BluetoothGatt g, int status, int newState) {
            log("GATT state " + status + " -> " + newState);
            if (newState == BluetoothProfile.STATE_CONNECTED) {
                g.discoverServices();
            } else if (newState == BluetoothProfile.STATE_DISCONNECTED) {
                bleReady = false;
                ready();   // readyOk stays false: cycle() must not proceed on a dead link
            }
        }

        @Override public void onServicesDiscovered(BluetoothGatt g, int status) {
            if (status != 0) { log("service discovery failed " + status); ready(); return; }
            writeChar = null; notifyChar = null;
            for (BluetoothGattService s : g.getServices()) {
                String su = s.getUuid().toString().toLowerCase();
                if (su.startsWith("00001800") || su.startsWith("00001801")) continue; // GAP/GATT
                for (BluetoothGattCharacteristic c : s.getCharacteristics()) {
                    int p = c.getProperties();
                    if (notifyChar == null && (p & BluetoothGattCharacteristic.PROPERTY_NOTIFY) != 0) notifyChar = c;
                    if (notifyChar == null && (p & BluetoothGattCharacteristic.PROPERTY_INDICATE) != 0) notifyChar = c;
                    if (writeChar == null && (p & BluetoothGattCharacteristic.PROPERTY_WRITE) != 0) writeChar = c;
                    if (writeChar == null && (p & BluetoothGattCharacteristic.PROPERTY_WRITE_NO_RESPONSE) != 0) writeChar = c;
                }
            }
            if (writeChar == null) {
                log("no write characteristic found");
                try { g.disconnect(); } catch (Exception ignored) {}
                ready();   // readyOk stays false -> cycle fails fast and rescans
                return;
            }
            readyOk = true;
            if (notifyChar == null) {
                notifyChar = writeChar;
                log("no separate notify char; using write char for notify");
            }
            log("write=" + writeChar.getUuid() + " notify=" + notifyChar.getUuid());
            g.setCharacteristicNotification(notifyChar, true);
            BluetoothGattDescriptor d = notifyChar.getDescriptor(CCCD);
            if (d != null) {
                d.setValue(BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE);
                g.writeDescriptor(d);
            } else {
                log("no CCCD - proceeding without notifications");
                g.requestMtu(185);
                scheduleFallback();
            }
        }

        @Override public void onDescriptorWrite(BluetoothGatt g, BluetoothGattDescriptor d, int status) {
            if (status != 0) log("descriptor write failed " + status);
            g.requestMtu(185);
            scheduleFallback();
        }

        @Override public void onMtuChanged(BluetoothGatt g, int mtu, int status) {
            BridgeService.this.mtu = mtu;
            ready();
        }

        @Override public void onCharacteristicChanged(BluetoothGatt g, BluetoothGattCharacteristic c) {
            byte[] v = c.getValue();
            if (v == null || v.length == 0) return;
            OutputStream out = clientOut;
            if (out != null) {
                try { out.write(v); out.flush(); }
                catch (Exception e) { log("tcp write failed: " + e); }
            }
        }

        private void ready() {
            CountDownLatch l = readyLatch;
            readyLatch = null;
            if (l != null) l.countDown();
        }
        private void scheduleFallback() {
            new Thread(() -> { sleep(4000); ready(); }, "mtu-fallback").start();
        }
    }

    void serverLoop() throws Exception {
        ServerSocket ss = new ServerSocket(PORT, 1, InetAddress.getByName("127.0.0.1"));
        ss.setSoTimeout(1000);
        log("TCP up on 127.0.0.1:" + PORT);
        try {
            while (bleReady) {
                Socket c;
                try { c = ss.accept(); }
                catch (SocketTimeoutException e) { continue; }
                log("tcp client connected");
                try {
                    client = c;
                    clientOut = c.getOutputStream();
                    pump(c);
                } finally {
                    log("tcp client gone");
                    try { if (clientOut != null) clientOut.close(); } catch (Exception ignored) {}
                    try { c.close(); } catch (Exception ignored) {}
                    client = null; clientOut = null;
                }
            }
        } finally {
            try { ss.close(); } catch (Exception ignored) {}
        }
    }

    void pump(Socket c) {
        try {
            InputStream in = c.getInputStream();
            byte[] buf = new byte[256];
            int n;
            while (bleReady && (n = in.read(buf)) > 0) {
                int chunk = Math.max(20, Math.min(180, mtu - 3));
                for (int off = 0; off < n; off += chunk) {
                    int len = Math.min(chunk, n - off);
                    byte[] piece = new byte[len];
                    System.arraycopy(buf, off, piece, 0, len);
                    writeChar.setValue(piece);
                    boolean ok = gatt.writeCharacteristic(writeChar);
                    if (!ok) log("BLE write failed at off=" + off);
                    sleep(30);
                }
            }
        } catch (Exception e) {
            log("pump end: " + e);
        }
    }

    void teardown() {
        bleReady = false;
        try { if (gatt != null) { gatt.disconnect(); gatt.close(); } } catch (Exception ignored) {}
        gatt = null;
        try { if (clientOut != null) clientOut.close(); } catch (Exception ignored) {}
        try { if (client != null) client.close(); } catch (Exception ignored) {}
        client = null; clientOut = null;
        writeChar = null; notifyChar = null;
    }

    @Override
    public void onDestroy() {
        teardown();
        super.onDestroy();
    }
}
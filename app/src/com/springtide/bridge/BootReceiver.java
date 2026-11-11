package com.springtide.bridge;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/**
 * Root-independent boot autostart (2026-11-11, v0.5). The old Termux boot
 * hook called `su -c am start-foreground-service ...` — dead since the
 * 11-09 OTA reboot removed Magisk, which left the bridge down for 2 days
 * and cost a field test. BOOT_COMPLETED is a sanctioned exemption for
 * launching connectedDevice-type foreground services on Android 15; the
 * system delivers this broadcast to receivers declared exported=false.
 * Termux's unprivileged boot-hook am call and the launcher icon remain as
 * fallbacks.
 */
public class BootReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context context, Intent intent) {
        if (!Intent.ACTION_BOOT_COMPLETED.equals(intent.getAction())) return;
        try {
            context.startForegroundService(new Intent(context, BridgeService.class));
        } catch (Exception ignored) {
            // blocked by platform policy — launcher icon / Termux hook are the fallbacks
        }
    }
}

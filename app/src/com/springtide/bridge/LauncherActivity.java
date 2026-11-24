package com.springtide.bridge;

import android.app.Activity;
import android.content.Intent;
import android.os.Bundle;
import android.widget.TextView;

/**
 * Tap-to-start entry point (2026-11-11, v0.5): the phone lost Magisk root at the
 * 11-09 OTA reboot, so the bridge can no longer be launched via
 * `su -c am start-foreground-service`. A user-visible activity starting its
 * own foreground service is always sanctioned by platform policy — this is
 * the guaranteed fallback when Termux's unprivileged `am start-foreground-
 * service` hits Android 15's background-FGS-launch wall.
 */
public class LauncherActivity extends Activity {
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        TextView tv = new TextView(this);
        tv.setPadding(32, 64, 32, 64);
        tv.setText("OBD Bridge starting...");
        setContentView(tv);
        try {
            startForegroundService(new Intent(this, BridgeService.class));
        } catch (Exception e) {
            // stay on screen with the error so a human sees what happened
            tv.setText("OBD Bridge start failed:\n" + e);
            return;
        }
        finish();
    }
}

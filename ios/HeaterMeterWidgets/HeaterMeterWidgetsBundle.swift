//  HeaterMeterWidgetsBundle.swift
//  Widget extension entry: hosts the cook Live Activity (Lock Screen + Dynamic Island).

import SwiftUI
import WidgetKit

@main
struct HeaterMeterWidgetsBundle: WidgetBundle {
    var body: some Widget {
        CookLiveActivity()
    }
}

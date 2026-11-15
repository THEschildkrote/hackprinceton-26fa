import AVFoundation
import UIKit

// speaks every instruction. the phone rides on the traveller's back, so this is the whole interface.
// play-and-record so the microphone can take a tap-to-talk sentence; works with the screen locked and
// through earbuds. while the traveller is talking it holds new lines back, so the mic never hears the app,
// and says them once the sentence is done.
@MainActor
final class Speech: NSObject, AVSpeechSynthesizerDelegate {
    static let shared = Speech()
    private let synth = AVSpeechSynthesizer()
    var rate: Float = AVSpeechUtteranceDefaultSpeechRate
    var onSpeaking: ((Bool) -> Void)?
    private(set) var speaking = false
    private var held: [String] = []
    var holding = false {
        didSet {
            guard !holding, oldValue else { return }
            let lines = held
            held = []
            for l in lines { say(l) }
        }
    }

    private override init() {
        super.init()
        synth.delegate = self
        configureAudio()
    }

    func configureAudio() {
        let s = AVAudioSession.sharedInstance()
        try? s.setCategory(.playAndRecord, mode: .default,
                           options: [.duckOthers, .defaultToSpeaker, .allowBluetooth, .allowBluetoothA2DP])
        try? s.setActive(true)
    }

    func say(_ text: String, interrupt: Bool = false) {
        let t = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !t.isEmpty else { return }
        if holding { held.append(t); return }
        if UIAccessibility.isVoiceOverRunning {
            // one voice, not two: let voiceover read it
            UIAccessibility.post(notification: .announcement, argument: t)
            return
        }
        if interrupt { synth.stopSpeaking(at: .immediate) }
        let u = AVSpeechUtterance(string: t)
        u.rate = rate
        u.postUtteranceDelay = 0.15
        synth.speak(u)
    }

    // the traveller tapped to talk: stop mid-sentence so they don't wait for the app to finish
    func hush() { synth.stopSpeaking(at: .immediate) }

    func say(_ lines: [String]) {
        for l in lines { say(l) }
    }

    nonisolated func speechSynthesizer(_ s: AVSpeechSynthesizer, didStart u: AVSpeechUtterance) {
        Task { @MainActor in self.setSpeaking(true) }
    }

    nonisolated func speechSynthesizer(_ s: AVSpeechSynthesizer, didFinish u: AVSpeechUtterance) {
        Task { @MainActor in if !self.synth.isSpeaking { self.setSpeaking(false) } }
    }

    nonisolated func speechSynthesizer(_ s: AVSpeechSynthesizer, didCancel u: AVSpeechUtterance) {
        Task { @MainActor in if !self.synth.isSpeaking { self.setSpeaking(false) } }
    }

    private func setSpeaking(_ v: Bool) {
        guard v != speaking else { return }
        speaking = v
        onSpeaking?(v)
    }
}

enum Haptic {
    static func tick() { UIImpactFeedbackGenerator(style: .rigid).impactOccurred() }
    static func success() { UINotificationFeedbackGenerator().notificationOccurred(.success) }
    static func warning() { UINotificationFeedbackGenerator().notificationOccurred(.warning) }
}

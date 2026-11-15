import AVFoundation
import Speech

// tap to talk: the whole screen is the button. the mic is off until the traveller taps, then it listens for
// one sentence and stops by itself: ~0.9 s after the last new word, or when the recognizer calls the
// sentence final. nothing heard in 6 s, or 15 s in all, also ends it. a second tap sends what was heard now.
// (it used to listen all the time and wait out every pause, which kept the recognizer running for the
// whole walk and made each command wait on the silence timer.)
@MainActor
final class Listener {
    private let recognizer = SFSpeechRecognizer(locale: Locale(identifier: "en-US"))
    private let engine = AVAudioEngine()
    private var request: SFSpeechAudioBufferRecognitionRequest?
    private var task: SFSpeechRecognitionTask?
    private var lastText = ""
    private var silence: Task<Void, Never>?
    private var limit: Task<Void, Never>?
    private(set) var listening = false

    var onUtterance: ((String) -> Void)?
    var onNothingHeard: (() -> Void)?
    var onListening: ((Bool) -> Void)?

    static let sentenceGapNs: UInt64 = 900_000_000
    static let nothingHeardNs: UInt64 = 6_000_000_000
    static let longestNs: UInt64 = 15_000_000_000

    static func requestAccess() async -> Bool {
        let speech = await withCheckedContinuation { cont in
            SFSpeechRecognizer.requestAuthorization { cont.resume(returning: $0 == .authorized) }
        }
        let mic = await AVAudioApplication.requestRecordPermission()
        return speech && mic
    }

    var available: Bool { recognizer?.isAvailable == true }

    // one sentence. false if the recognizer or the mic can't start
    @discardableResult
    func listen() -> Bool {
        guard !listening, let recognizer, recognizer.isAvailable else { return false }
        let req = SFSpeechAudioBufferRecognitionRequest()
        req.shouldReportPartialResults = true
        if recognizer.supportsOnDeviceRecognition { req.requiresOnDeviceRecognition = true }
        req.taskHint = .search
        req.contextualStrings = ["take me to", "how do I get to", "where am I", "repeat", "how far",
                                 "I need help", "the door is locked", "elevator is out", "I'm lost", "stop",
                                 "yes", "no", "demo corner", "straight line demo", "the finish"]
        request = req
        lastText = ""
        let input = engine.inputNode
        let format = input.outputFormat(forBus: 0)
        input.removeTap(onBus: 0)
        input.installTap(onBus: 0, bufferSize: 1024, format: format) { [weak req] buf, _ in req?.append(buf) }
        engine.prepare()
        do { try engine.start() } catch {
            input.removeTap(onBus: 0)
            request = nil
            return false
        }
        listening = true
        onListening?(true)
        task = recognizer.recognitionTask(with: req) { [weak self] result, error in
            let text = result?.bestTranscription.formattedString
            let final = result?.isFinal == true
            let failed = error != nil
            Task { @MainActor in self?.handle(text: text, final: final, failed: failed) }
        }
        limit = Task { [weak self] in
            try? await Task.sleep(nanoseconds: Self.nothingHeardNs)
            guard let self, !Task.isCancelled, self.listening else { return }
            if self.lastText.isEmpty { self.finish(); return }
            try? await Task.sleep(nanoseconds: Self.longestNs - Self.nothingHeardNs)
            guard !Task.isCancelled, self.listening else { return }
            self.finish()
        }
        return true
    }

    // the second tap: send what was heard so far
    func finishNow() { finish() }

    private func handle(text: String?, final: Bool, failed: Bool) {
        guard listening else { return }
        if let text, !text.isEmpty, text != lastText {
            lastText = text
            silence?.cancel()
            silence = Task { [weak self] in
                try? await Task.sleep(nanoseconds: Self.sentenceGapNs)
                guard !Task.isCancelled else { return }
                self?.finish()
            }
        }
        if final || failed { finish() }
    }

    private func finish() {
        guard listening else { return }
        listening = false
        let t = lastText.trimmingCharacters(in: .whitespacesAndNewlines)
        silence?.cancel()
        limit?.cancel()
        task?.cancel()
        task = nil
        request?.endAudio()
        request = nil
        if engine.isRunning { engine.stop() }
        engine.inputNode.removeTap(onBus: 0)
        onListening?(false)
        if t.isEmpty { onNothingHeard?() } else { onUtterance?(t) }
    }
}

import AVFoundation
import Speech

// hands-free commands while walking: on-device speech recognition, always listening, paused while the app
// itself is talking. an utterance ends after ~1.2 s without new words; then the recognizer restarts
// (ios also caps one recognition task at about a minute).
@MainActor
final class Listener {
    private let recognizer = SFSpeechRecognizer(locale: Locale(identifier: "en-US"))
    private let engine = AVAudioEngine()
    private var request: SFSpeechAudioBufferRecognitionRequest?
    private var task: SFSpeechRecognitionTask?
    private var lastText = ""
    private var silence: Task<Void, Never>?
    private(set) var listening = false
    private var paused = false
    var onUtterance: ((String) -> Void)?

    static func requestAccess() async -> Bool {
        let speech = await withCheckedContinuation { cont in
            SFSpeechRecognizer.requestAuthorization { cont.resume(returning: $0 == .authorized) }
        }
        let mic = await AVAudioApplication.requestRecordPermission()
        return speech && mic
    }

    func start() {
        guard !listening, let recognizer, recognizer.isAvailable else { return }
        listening = true
        paused = false
        begin()
    }

    func stop() {
        listening = false
        end()
    }

    // while the app speaks, stop the mic so it doesn't transcribe its own voice
    func setPaused(_ p: Bool) {
        guard listening, p != paused else { return }
        paused = p
        if p { end() } else { begin() }
    }

    private func begin() {
        guard let recognizer else { return }
        let req = SFSpeechAudioBufferRecognitionRequest()
        req.shouldReportPartialResults = true
        if recognizer.supportsOnDeviceRecognition { req.requiresOnDeviceRecognition = true }
        req.taskHint = .search
        req.contextualStrings = ["where am I", "repeat", "how far", "I need help", "the door is locked",
                                 "elevator is out", "I'm lost", "stop", "yes", "no"]
        request = req
        lastText = ""
        let input = engine.inputNode
        let format = input.outputFormat(forBus: 0)
        input.removeTap(onBus: 0)
        input.installTap(onBus: 0, bufferSize: 1024, format: format) { [weak req] buf, _ in req?.append(buf) }
        engine.prepare()
        do { try engine.start() } catch { listening = false; return }
        task = recognizer.recognitionTask(with: req) { [weak self] result, error in
            let text = result?.bestTranscription.formattedString
            let failed = error != nil
            Task { @MainActor in self?.handle(text: text, failed: failed) }
        }
    }

    private func end() {
        silence?.cancel()
        task?.cancel()
        task = nil
        request?.endAudio()
        request = nil
        if engine.isRunning { engine.stop() }
        engine.inputNode.removeTap(onBus: 0)
    }

    private func handle(text: String?, failed: Bool) {
        if let text, !text.isEmpty, text != lastText {
            lastText = text
            silence?.cancel()
            silence = Task { [weak self] in
                try? await Task.sleep(nanoseconds: 1_200_000_000)
                guard !Task.isCancelled else { return }
                self?.finishUtterance()
            }
        } else if failed, listening, !paused {
            // the task ended (timeout or error): start a fresh one
            end()
            begin()
        }
    }

    private func finishUtterance() {
        let t = lastText.trimmingCharacters(in: .whitespacesAndNewlines)
        end()
        if !t.isEmpty { onUtterance?(t) }
        if listening, !paused { begin() }
    }
}

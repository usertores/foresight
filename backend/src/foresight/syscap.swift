// syscap: the Mac's audio output (what Zoom, Meet… play) as 16 kHz mono float32 PCM on stdout.
//
// macOS has no loopback device like Windows' WASAPI, so this captures the system audio with
// ScreenCaptureKit (macOS 13+). Built and run by macaudio.py. Status lines go to stderr:
// "ready" once capturing. Exits when stdin closes (the backend went away).
//
//   syscap           capture until stdin closes
//   syscap --check   exit 0 if allowed to capture; otherwise ask macOS for permission, exit 2
//
// Exit codes: 2 = no Screen & System Audio Recording permission, 3 = capture failed.

import CoreGraphics
import CoreMedia
import Foundation
import ScreenCaptureKit

let sampleRate = 16000

func log(_ s: String) {
    FileHandle.standardError.write((s + "\n").data(using: .utf8)!)
}

func fail(_ s: String, _ code: Int32) -> Never {
    log(s)
    exit(code)
}

final class AudioOut: NSObject, SCStreamOutput, SCStreamDelegate {
    private var warned = false

    func stream(_ stream: SCStream, didOutputSampleBuffer sb: CMSampleBuffer,
                of type: SCStreamOutputType) {
        guard type == .audio, sb.isValid, CMSampleBufferGetNumSamples(sb) > 0 else { return }
        guard let fmt = CMSampleBufferGetFormatDescription(sb),
              let asbd = CMAudioFormatDescriptionGetStreamBasicDescription(fmt)?.pointee
        else { return }
        if Int(asbd.mSampleRate) != sampleRate && !warned {
            warned = true
            log("warning: got \(asbd.mSampleRate) Hz")
        }

        var needed = 0
        CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
            sb, bufferListSizeNeededOut: &needed, bufferListOut: nil, bufferListSize: 0,
            blockBufferAllocator: nil, blockBufferMemoryAllocator: nil, flags: 0,
            blockBufferOut: nil)
        let raw = UnsafeMutableRawPointer.allocate(
            byteCount: needed, alignment: MemoryLayout<AudioBufferList>.alignment)
        defer { raw.deallocate() }
        let abl = raw.bindMemory(to: AudioBufferList.self, capacity: 1)
        var block: CMBlockBuffer?
        let status = CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
            sb, bufferListSizeNeededOut: nil, bufferListOut: abl, bufferListSize: needed,
            blockBufferAllocator: nil, blockBufferMemoryAllocator: nil,
            flags: kCMSampleBufferFlag_AudioBufferList_Assure16ByteAlignment,
            blockBufferOut: &block)
        guard status == noErr else { return }

        // Float32, non-interleaved: one buffer per channel. Mix down to mono if needed.
        let buffers = UnsafeMutableAudioBufferListPointer(abl)
        guard let first = buffers.first, let p0 = first.mData else { return }
        let frames = Int(first.mDataByteSize) / MemoryLayout<Float>.size
        if buffers.count == 1 {
            FileHandle.standardOutput.write(Data(bytes: p0, count: frames * 4))
            return
        }
        var mono = [Float](repeating: 0, count: frames)
        for b in buffers {
            guard let p = b.mData?.assumingMemoryBound(to: Float.self) else { continue }
            for i in 0..<frames { mono[i] += p[i] }
        }
        let scale = 1 / Float(buffers.count)
        for i in 0..<frames { mono[i] *= scale }
        mono.withUnsafeBytes { FileHandle.standardOutput.write(Data($0)) }
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        fail("capture stopped: \(error.localizedDescription)", 3)
    }
}

/// Video frames are required by ScreenCaptureKit but unused: take them and drop them.
final class VideoSink: NSObject, SCStreamOutput {
    func stream(_ stream: SCStream, didOutputSampleBuffer sb: CMSampleBuffer,
                of type: SCStreamOutputType) {}
}

if CommandLine.arguments.contains("--check") {
    if CGPreflightScreenCaptureAccess() { exit(0) }
    _ = CGRequestScreenCaptureAccess()  // shows the system prompt the first time
    exit(2)
}

// The backend closes our stdin (or dies) when the class ends.
Thread {
    while !FileHandle.standardInput.availableData.isEmpty {}
    exit(0)
}.start()

let audio = AudioOut()
let video = VideoSink()
var stream: SCStream?

Task {
    let content: SCShareableContent
    do {
        content = try await SCShareableContent.excludingDesktopWindows(
            false, onScreenWindowsOnly: true)
    } catch {
        fail("no permission: \(error.localizedDescription)", 2)
    }
    guard let display = content.displays.first else { fail("no display", 3) }

    let cfg = SCStreamConfiguration()
    cfg.capturesAudio = true
    cfg.sampleRate = sampleRate
    cfg.channelCount = 1
    cfg.excludesCurrentProcessAudio = true
    cfg.width = 2
    cfg.height = 2
    cfg.minimumFrameInterval = CMTime(value: 1, timescale: 1)
    cfg.queueDepth = 3

    let filter = SCContentFilter(display: display, excludingApplications: [],
                                 exceptingWindows: [])
    let s = SCStream(filter: filter, configuration: cfg, delegate: audio)
    do {
        try s.addStreamOutput(audio, type: .audio,
                              sampleHandlerQueue: DispatchQueue(label: "syscap.audio"))
        try s.addStreamOutput(video, type: .screen,
                              sampleHandlerQueue: DispatchQueue(label: "syscap.video"))
        try await s.startCapture()
    } catch {
        fail("could not start capture: \(error.localizedDescription)", 3)
    }
    stream = s
    log("ready")
}

dispatchMain()

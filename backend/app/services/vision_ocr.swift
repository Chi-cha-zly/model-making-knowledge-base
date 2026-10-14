// Local-only Vision OCR. JSON over stdin/stdout; no network or image modification.
import Foundation
import Vision

func run() throws {
    let data = FileHandle.standardInput.readDataToEndOfFile()
    guard let input = try JSONSerialization.jsonObject(with:data) as? [String:Any],
          let path = input["path"] as? String else { throw NSError(domain:"OCRInput",code:1) }
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    // Chinese must precede English for the multilingual Chinese recognizer.
    let supported = try request.supportedRecognitionLanguages()
    let languages = input["languages"] as? [String] ?? ["zh-Hans", "zh-Hant", "en-US"]
    request.recognitionLanguages = languages.filter { supported.contains($0) }
    guard request.recognitionLanguages.count == languages.count else { throw NSError(domain:"OCRLanguages",code:1) }
    request.usesLanguageCorrection = input["language_correction"] as? Bool ?? true
    var roi = CGRect(x:0,y:0,width:1,height:1)
    if let r = input["region"] as? [Double], r.count == 4 {
        roi = CGRect(x:r[0],y:r[1],width:r[2],height:r[3])
        request.regionOfInterest = roi
    }
    try VNImageRequestHandler(url:URL(fileURLWithPath:path),options:[:]).perform([request])
    let observations: [[String:Any]] = (request.results ?? []).compactMap { o in
        guard let candidate = o.topCandidates(1).first else { return nil }
        let b = o.boundingBox
        // Vision's ROI coordinates are relative to the requested region.
        return ["text":candidate.string,"confidence":candidate.confidence,
                "x":roi.minX+b.minX*roi.width,"y":roi.minY+b.minY*roi.height,
                "w":b.width*roi.width,"h":b.height*roi.height]
    }
    let output: [String:Any] = ["engine":"apple-vision","revision":request.revision,
                              "observations":observations]
    FileHandle.standardOutput.write(try JSONSerialization.data(withJSONObject:output,options:[.sortedKeys]))
}
do { try run() } catch {
    // Native failures must not be confused with a valid image without text.
    FileHandle.standardError.write(Data("Local Vision OCR failed\n".utf8))
    exit(1)
}

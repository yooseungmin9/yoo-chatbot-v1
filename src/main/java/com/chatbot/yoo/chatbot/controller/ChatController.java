package com.chatbot.yoo.chatbot.controller;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.core.io.ByteArrayResource;
import org.springframework.http.*;
import org.springframework.http.client.SimpleClientHttpRequestFactory;
import org.springframework.stereotype.Controller;
import org.springframework.util.LinkedMultiValueMap;
import org.springframework.util.MultiValueMap;
import org.springframework.web.bind.annotation.*;
import org.springframework.web.client.HttpStatusCodeException;
import org.springframework.web.client.RestClientException;
import org.springframework.web.client.RestTemplate;
import org.springframework.web.multipart.MultipartFile;

import java.nio.charset.StandardCharsets;
import java.util.Arrays;
import java.util.Map;
import java.util.logging.Logger;

@Controller
public class ChatController {

    private static final Logger log = Logger.getLogger(ChatController.class.getName());

    // FastAPI 주소 (application.properties: fastapi.url=http://localhost:8000)
    @Value("${fastapi.chat:http://localhost:8000}")
    private String FASTAPI_URL;

    private static RestTemplate createRestTemplate() {
        SimpleClientHttpRequestFactory f = new SimpleClientHttpRequestFactory();
        f.setConnectTimeout(5_000);
        f.setReadTimeout(180_000);
        return new RestTemplate(f);
    }
    private final RestTemplate rest = createRestTemplate();

    // 챗봇 페이지
    @GetMapping("/chat")
    public String chatPage() { return "/chat"; }

    // === Chat: POST /api/chat → FastAPI /chat ===
    @PostMapping(value = "/api/chat", consumes = MediaType.APPLICATION_JSON_VALUE, produces = MediaType.APPLICATION_JSON_VALUE)
    @ResponseBody
    public ResponseEntity<String> proxyChat(@RequestBody Map<String, Object> body) {
        final String url = FASTAPI_URL + "/chat";
        HttpHeaders headers = new HttpHeaders();
        headers.setContentType(MediaType.APPLICATION_JSON);
        headers.setAccept(Arrays.asList(MediaType.APPLICATION_JSON));

        try {
            return rest.postForEntity(url, new HttpEntity<>(body, headers), String.class);
        } catch (HttpStatusCodeException ex) {
            return ResponseEntity.status(ex.getStatusCode())
                    .contentType(ex.getResponseHeaders() != null
                            ? ex.getResponseHeaders().getContentType()
                            : MediaType.APPLICATION_JSON)
                    .body(ex.getResponseBodyAsString());
        } catch (RestClientException e) {
            log.severe("proxyChat upstream error: " + e.getMessage());
            return ResponseEntity.status(HttpStatus.BAD_GATEWAY)
                    .contentType(MediaType.APPLICATION_JSON)
                    .body("{\"error\":\"게이트웨이 오류: FastAPI /chat 접속 실패\"}");
        }
    }

    // === Reset: POST /api/reset → FastAPI /reset ===
    @PostMapping(value = "/api/reset", produces = MediaType.APPLICATION_JSON_VALUE)
    @ResponseBody
    public ResponseEntity<String> proxyReset() {
        try {
            return rest.postForEntity(FASTAPI_URL + "/reset", null, String.class);
        } catch (HttpStatusCodeException ex) {
            return ResponseEntity.status(ex.getStatusCode())
                    .contentType(MediaType.APPLICATION_JSON)
                    .body(ex.getResponseBodyAsString());
        } catch (RestClientException e) {
            log.severe("proxyReset upstream error: " + e.getMessage());
            return ResponseEntity.status(HttpStatus.BAD_GATEWAY)
                    .contentType(MediaType.APPLICATION_JSON)
                    .body("{\"message\":\"게이트웨이 오류: FastAPI /reset 접속 실패\"}");
        }
    }

    // === STT: POST multipart /api/stt → FastAPI /api/stt ===
    @PostMapping(value = "/api/stt", consumes = MediaType.MULTIPART_FORM_DATA_VALUE, produces = MediaType.APPLICATION_JSON_VALUE)
    @ResponseBody
    public ResponseEntity<String> proxyStt(
            @RequestParam("audio_file") MultipartFile audioFile,
            @RequestParam(name = "lang", defaultValue = "Kor") String lang
    ) {
        final String url = FASTAPI_URL + "/api/stt?lang=" + lang;

        // 파일 파트에 Content-Disposition/Type 명시
        HttpHeaders fileHdr = new HttpHeaders();
        fileHdr.setContentType(MediaType.APPLICATION_OCTET_STREAM);
        fileHdr.setContentDisposition(ContentDisposition.formData()
                .name("audio_file")
                .filename(audioFile.getOriginalFilename())
                .build());
        ByteArrayResource fileRes = new ByteArrayResource(toBytes(audioFile)) {
            @Override public String getFilename() { return audioFile.getOriginalFilename(); }
        };
        HttpEntity<ByteArrayResource> fileEntity = new HttpEntity<>(fileRes, fileHdr);

        MultiValueMap<String, Object> body = new LinkedMultiValueMap<>();
        body.add("audio_file", fileEntity);

        HttpHeaders headers = new HttpHeaders();
        headers.setContentType(MediaType.MULTIPART_FORM_DATA);
        headers.setAccept(Arrays.asList(MediaType.APPLICATION_JSON));

        try {
            return rest.postForEntity(url, new HttpEntity<>(body, headers), String.class);
        } catch (HttpStatusCodeException ex) { // FastAPI 4xx/5xx 그대로
            MediaType ct = ex.getResponseHeaders() != null ? ex.getResponseHeaders().getContentType() : MediaType.APPLICATION_JSON;
            return ResponseEntity.status(ex.getStatusCode()).contentType(ct).body(ex.getResponseBodyAsString());
        } catch (RestClientException e) {
            log.severe("proxyStt upstream error: " + e.getMessage());
            return ResponseEntity.status(HttpStatus.BAD_GATEWAY)
                    .contentType(MediaType.APPLICATION_JSON)
                    .body("{\"error\":\"게이트웨이 오류: FastAPI /api/stt 접속 실패\"}");
        }
    }

    // === TTS: POST JSON /api/tts → FastAPI /api/tts ===
    @PostMapping(value = "/api/tts", consumes = MediaType.APPLICATION_JSON_VALUE)
    @ResponseBody
    public ResponseEntity<byte[]> proxyTtsPost(@RequestBody Map<String, Object> body) {
        final String url = FASTAPI_URL + "/api/tts";
        try {
            HttpHeaders hdr = new HttpHeaders();
            hdr.setContentType(MediaType.APPLICATION_JSON);
            hdr.setAccept(Arrays.asList(
                    MediaType.valueOf("audio/mpeg"),
                    MediaType.valueOf("audio/ogg"),
                    MediaType.valueOf("audio/wav"),
                    MediaType.APPLICATION_JSON
            ));

            ResponseEntity<byte[]> res = rest.exchange(url, HttpMethod.POST, new HttpEntity<>(body, hdr), byte[].class);

            HttpHeaders out = new HttpHeaders();
            MediaType ct = res.getHeaders().getContentType();
            out.setContentType(ct != null ? ct : MediaType.APPLICATION_OCTET_STREAM);
            String disp = res.getHeaders().getFirst("Content-Disposition");
            out.set("Content-Disposition", disp != null ? disp : "inline; filename=\"speech.bin\"");
            out.setCacheControl(CacheControl.noCache());

            return new ResponseEntity<>(res.getBody(), out, res.getStatusCode());

        } catch (HttpStatusCodeException ex) {
            // FastAPI가 JSON 에러 반환 시 그대로 전달
            HttpHeaders out = new HttpHeaders();
            out.setContentType(MediaType.APPLICATION_JSON);
            return new ResponseEntity<>(ex.getResponseBodyAsByteArray(), out, ex.getStatusCode());
        } catch (RestClientException e) {
            return jsonError("{\"error\":\"Gateway error: cannot reach FastAPI /api/tts\"}", HttpStatus.BAD_GATEWAY);
        } catch (Exception e) {
            return jsonError("{\"error\":\"Unexpected error in /api/tts\"}", HttpStatus.INTERNAL_SERVER_ERROR);
        }
    }

    // === 유틸 ===
    private byte[] toBytes(MultipartFile f) {
        try { return f.getBytes(); }
        catch (Exception e) { throw new RuntimeException("파일 읽기 실패", e); }
    }

    private ResponseEntity<byte[]> jsonError(String json, HttpStatus status) {
        HttpHeaders hdr = new HttpHeaders();
        hdr.setContentType(MediaType.APPLICATION_JSON);
        return new ResponseEntity<>(json.getBytes(StandardCharsets.UTF_8), hdr, status);
    }
}
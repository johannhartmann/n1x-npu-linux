#include "n1x_compiler.h"

#include <ctype.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#include <map>
#include <memory>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

namespace {

/* IEEE-754 FP32 -> FP16 conversion (round-to-nearest-even) */
uint16_t fp32_to_fp16(float value) {
    uint32_t fbits = 0;
    memcpy(&fbits, &value, sizeof(fbits));
    uint32_t sign = (fbits >> 16) & 0x8000U;
    int32_t exp = static_cast<int32_t>((fbits >> 23) & 0xffU) - 127 + 15;
    uint32_t mant = fbits & 0x007fffffU;

    if (exp <= 0) {
        if (exp < -10) {
            return static_cast<uint16_t>(sign);
        }
        mant |= 0x00800000U;
        uint32_t shift = static_cast<uint32_t>(14 - exp);
        uint32_t half_mant = mant >> shift;
        uint32_t rem = mant & ((1U << shift) - 1U);
        uint32_t halfway = 1U << (shift - 1U);
        if (rem > halfway || (rem == halfway && (half_mant & 1U))) {
            ++half_mant;
        }
        return static_cast<uint16_t>(sign | half_mant);
    } else if (exp == 0xff - 127 + 15) {
        if (mant == 0) {
            return static_cast<uint16_t>(sign | 0x7c00U);
        }
        return static_cast<uint16_t>(sign | 0x7c00U | (mant >> 13) | 1U);
    } else {
        uint32_t half_mant = mant >> 13;
        uint32_t rem = mant & 0x1fffU;
        if (rem > 0x1000U || (rem == 0x1000U && (half_mant & 1U))) {
            ++half_mant;
            if (half_mant == 0x0400U) {
                half_mant = 0;
                ++exp;
            }
        }
        if (exp >= 31) {
            return static_cast<uint16_t>(sign | 0x7c00U);
        }
        return static_cast<uint16_t>(sign | (static_cast<uint32_t>(exp) << 10) | half_mant);
    }
}

std::vector<uint8_t> base64_decode(const std::string &in) {
    static const int8_t kDecTable[256] = {
        -1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,
        -1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,
        -1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,62,-1,-1,-1,63,
        52,53,54,55,56,57,58,59,60,61,-1,-1,-1,-2,-1,-1,
        -1, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9,10,11,12,13,14,
        15,16,17,18,19,20,21,22,23,24,25,-1,-1,-1,-1,-1,
        -1,26,27,28,29,30,31,32,33,34,35,36,37,38,39,40,
        41,42,43,44,45,46,47,48,49,50,51,-1,-1,-1,-1,-1,
        -1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,
        -1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,
        -1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,
        -1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,
        -1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,
        -1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,
        -1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,
        -1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,
    };
    std::vector<uint8_t> out;
    out.reserve((in.size() * 3) / 4);
    uint32_t val = 0;
    int valb = -8;
    for (unsigned char c : in) {
        int8_t d = kDecTable[c];
        if (d == -1) continue;
        if (d == -2) break;
        val = (val << 6) + static_cast<uint32_t>(d);
        valb += 6;
        if (valb >= 0) {
            out.push_back(static_cast<uint8_t>((val >> valb) & 0xffU));
            valb -= 8;
        }
    }
    return out;
}

/* Minimal recursive-descent JSON parser */
struct JsonValue {
    enum Type { kNull, kBool, kNumber, kString, kArray, kObject };
    Type type = kNull;
    bool bool_val = false;
    double num_val = 0.0;
    std::string str_val;
    std::vector<JsonValue> arr_val;
    std::map<std::string, JsonValue> obj_val;

    bool is_null() const { return type == kNull; }
    bool is_bool() const { return type == kBool; }
    bool is_number() const { return type == kNumber; }
    bool is_string() const { return type == kString; }
    bool is_array() const { return type == kArray; }
    bool is_object() const { return type == kObject; }

    bool has(const std::string &key) const {
        return type == kObject && obj_val.find(key) != obj_val.end();
    }

    const JsonValue &at(const std::string &key) const {
        static const JsonValue kEmpty;
        if (type != kObject) return kEmpty;
        auto it = obj_val.find(key);
        return it != obj_val.end() ? it->second : kEmpty;
    }

    std::string str_or(const std::string &key, const std::string &def = "") const {
        const auto &v = at(key);
        return v.is_string() ? v.str_val : def;
    }

    int64_t int_or(const std::string &key, int64_t def = 0) const {
        const auto &v = at(key);
        return v.is_number() ? static_cast<int64_t>(v.num_val) : def;
    }

    double num_or(const std::string &key, double def = 0.0) const {
        const auto &v = at(key);
        return v.is_number() ? v.num_val : def;
    }

    bool bool_or(const std::string &key, bool def = false) const {
        const auto &v = at(key);
        if (v.is_bool()) return v.bool_val;
        if (v.is_number()) return v.num_val != 0.0;
        return def;
    }
};

class JsonParser {
public:
    explicit JsonParser(const char *src) : src_(src), pos_(0) {}

    JsonValue parse() {
        skip_ws();
        JsonValue v = parse_value();
        skip_ws();
        if (src_[pos_] != '\0') {
            throw std::runtime_error("unexpected trailing characters in JSON");
        }
        return v;
    }

private:
    const char *src_;
    size_t pos_;

    void skip_ws() {
        while (src_[pos_] == ' ' || src_[pos_] == '\t' || src_[pos_] == '\n' || src_[pos_] == '\r') {
            ++pos_;
        }
    }

    char peek() {
        skip_ws();
        return src_[pos_];
    }

    JsonValue parse_value() {
        char c = peek();
        if (c == '{') return parse_object();
        if (c == '[') return parse_array();
        if (c == '"') {
            JsonValue v;
            v.type = JsonValue::kString;
            v.str_val = parse_string();
            return v;
        }
        if (c == 't' || c == 'f') return parse_bool();
        if (c == 'n') return parse_null();
        if (c == '-' || isdigit(static_cast<unsigned char>(c))) return parse_number();
        throw std::runtime_error("unexpected character in JSON");
    }

    std::string parse_string() {
        skip_ws();
        if (src_[pos_] != '"') throw std::runtime_error("expected '\"'");
        ++pos_;
        std::string out;
        while (src_[pos_] != '\0' && src_[pos_] != '"') {
            char c = src_[pos_++];
            if (c == '\\') {
                char esc = src_[pos_++];
                switch (esc) {
                    case '"': out.push_back('"'); break;
                    case '\\': out.push_back('\\'); break;
                    case '/': out.push_back('/'); break;
                    case 'b': out.push_back('\b'); break;
                    case 'f': out.push_back('\f'); break;
                    case 'n': out.push_back('\n'); break;
                    case 'r': out.push_back('\r'); break;
                    case 't': out.push_back('\t'); break;
                    default: out.push_back(esc); break;
                }
            } else {
                out.push_back(c);
            }
        }
        if (src_[pos_] != '"') throw std::runtime_error("unterminated string in JSON");
        ++pos_;
        return out;
    }

    JsonValue parse_number() {
        skip_ws();
        char *endptr = nullptr;
        double val = strtod(src_ + pos_, &endptr);
        if (endptr == src_ + pos_) throw std::runtime_error("invalid number in JSON");
        pos_ = static_cast<size_t>(endptr - src_);
        JsonValue v;
        v.type = JsonValue::kNumber;
        v.num_val = val;
        return v;
    }

    JsonValue parse_bool() {
        skip_ws();
        JsonValue v;
        v.type = JsonValue::kBool;
        if (strncmp(src_ + pos_, "true", 4) == 0) {
            v.bool_val = true;
            pos_ += 4;
            return v;
        }
        if (strncmp(src_ + pos_, "false", 5) == 0) {
            v.bool_val = false;
            pos_ += 5;
            return v;
        }
        throw std::runtime_error("invalid boolean in JSON");
    }

    JsonValue parse_null() {
        skip_ws();
        if (strncmp(src_ + pos_, "null", 4) == 0) {
            pos_ += 4;
            return JsonValue{};
        }
        throw std::runtime_error("invalid null in JSON");
    }

    JsonValue parse_array() {
        skip_ws();
        ++pos_; /* '[' */
        JsonValue v;
        v.type = JsonValue::kArray;
        if (peek() == ']') {
            ++pos_;
            return v;
        }
        while (true) {
            v.arr_val.push_back(parse_value());
            char c = peek();
            if (c == ']') {
                ++pos_;
                break;
            }
            if (c != ',') throw std::runtime_error("expected ',' or ']' in array");
            ++pos_;
        }
        return v;
    }

    JsonValue parse_object() {
        skip_ws();
        ++pos_; /* '{' */
        JsonValue v;
        v.type = JsonValue::kObject;
        if (peek() == '}') {
            ++pos_;
            return v;
        }
        while (true) {
            std::string key = parse_string();
            if (peek() != ':') throw std::runtime_error("expected ':' after object key");
            ++pos_;
            v.obj_val[key] = parse_value();
            char c = peek();
            if (c == '}') {
                ++pos_;
                break;
            }
            if (c != ',') throw std::runtime_error("expected ',' or '}' in object");
            ++pos_;
        }
        return v;
    }
};

uint8_t parse_dtype_code(const JsonValue &v, uint8_t default_code) {
    if (v.is_number()) return static_cast<uint8_t>(v.num_val);
    if (!v.is_string()) return default_code;
    const std::string &s = v.str_val;
    if (s == "float" || s == "fp32" || s == "FLOAT") return N1X_DLA_DATA_TYPE_FLOAT;
    if (s == "half" || s == "fp16" || s == "HALF") return N1X_DLA_DATA_TYPE_HALF;
    if (s == "int16" || s == "INT16") return N1X_DLA_DATA_TYPE_INT16;
    if (s == "int8" || s == "INT8") return N1X_DLA_DATA_TYPE_INT8;
    if (s == "uint8" || s == "UINT8") return N1X_DLA_DATA_TYPE_UINT8;
    if (s == "uint16" || s == "UINT16") return N1X_DLA_DATA_TYPE_UINT16;
    return default_code;
}

uint8_t parse_format_code(const JsonValue &v, uint8_t default_code) {
    if (v.is_number()) return static_cast<uint8_t>(v.num_val);
    if (!v.is_string()) return default_code;
    const std::string &s = v.str_val;
    if (s == "nchw" || s == "NCHW") return N1X_DLA_DATA_FORMAT_NCHW;
    if (s == "nhwc" || s == "NHWC") return N1X_DLA_DATA_FORMAT_NHWC;
    if (s == "ncxhwx" || s == "NCxHWx" || s == "chw16" || s == "chw32") return N1X_DLA_DATA_FORMAT_NCxHWx;
    return default_code;
}

uint8_t parse_bias_mode(const JsonValue &v, bool has_bias) {
    if (v.is_number()) return static_cast<uint8_t>(v.num_val);
    if (v.is_string()) {
        const std::string &s = v.str_val;
        if (s == "none" || s == "NONE") return N1X_DLA_BIAS_NONE;
        if (s == "uniform" || s == "UNIFORM") return N1X_DLA_BIAS_UNIFORM;
        if (s == "channel" || s == "CHANNEL") return N1X_DLA_BIAS_CHANNEL;
        if (s == "elementwise" || s == "ELEMENTWISE") return N1X_DLA_BIAS_ELEMENTWISE;
    }
    return has_bias ? N1X_DLA_BIAS_CHANNEL : N1X_DLA_BIAS_NONE;
}

uint8_t parse_activation_type(const JsonValue &v) {
    if (v.is_number()) return static_cast<uint8_t>(v.num_val);
    if (v.is_string()) {
        const std::string &s = v.str_val;
        if (s == "relu" || s == "RELU" || s == "Relu") return 0;
        if (s == "sigmoid" || s == "SIGMOID" || s == "Sigmoid") return 1;
        if (s == "tanh" || s == "TANH" || s == "Tanh") return 2;
        if (s == "clip" || s == "CLIP" || s == "Clip" || s == "relu6" || s == "ReLU6") return 5;
        if (s == "leaky_relu" || s == "LEAKY_RELU" || s == "LeakyRelu") return 6;
    }
    return 0;
}

uint8_t parse_pooling_type(const JsonValue &v) {
    if (v.is_number()) return static_cast<uint8_t>(v.num_val);
    if (v.is_string()) {
        const std::string &s = v.str_val;
        if (s == "min" || s == "MIN" || s == "MinPool") return N1X_DLA_POOLING_MIN;
        if (s == "max" || s == "MAX" || s == "MaxPool") return N1X_DLA_POOLING_MAX;
        if (s == "average" || s == "avg" || s == "AVERAGE" || s == "AveragePool") return N1X_DLA_POOLING_AVERAGE;
    }
    return N1X_DLA_POOLING_MAX;
}

uint8_t parse_ew_op(const JsonValue &v) {
    if (v.is_number()) return static_cast<uint8_t>(v.num_val);
    if (v.is_string()) {
        const std::string &s = v.str_val;
        if (s == "sum" || s == "add" || s == "SUM" || s == "Add") return N1X_DLA_EW_SUM;
        if (s == "prod" || s == "mul" || s == "PROD" || s == "Mul") return N1X_DLA_EW_PROD;
        if (s == "min" || s == "MIN" || s == "Min") return N1X_DLA_EW_MIN;
        if (s == "max" || s == "MAX" || s == "Max") return N1X_DLA_EW_MAX;
        if (s == "sub" || s == "SUB" || s == "Sub") return N1X_DLA_EW_SUB;
    }
    return N1X_DLA_EW_SUM; /* default SUM */
}

uint8_t parse_scale_mode(const JsonValue &v) {
    if (v.is_number()) return static_cast<uint8_t>(v.num_val);
    if (v.is_string()) {
        const std::string &s = v.str_val;
        if (s == "uniform" || s == "UNIFORM") return N1X_DLA_SCALE_UNIFORM;
        if (s == "channel" || s == "CHANNEL") return N1X_DLA_SCALE_CHANNEL;
        if (s == "elementwise" || s == "ELEMENTWISE") return N1X_DLA_SCALE_ELEMENTWISE;
    }
    return N1X_DLA_SCALE_CHANNEL;
}

uint8_t parse_bn_mode(const JsonValue &v) {
    if (v.is_number()) return static_cast<uint8_t>(v.num_val);
    if (v.is_string()) {
        const std::string &s = v.str_val;
        if (s == "uniform" || s == "UNIFORM") return N1X_DLA_BN_UNIFORM;
        if (s == "channel" || s == "CHANNEL") return N1X_DLA_BN_CHANNEL;
    }
    return N1X_DLA_BN_CHANNEL;
}

N1xDlaDims2 parse_dims2(const JsonValue &v, int32_t def_h, int32_t def_w) {
    if (v.is_array() && v.arr_val.size() >= 2) {
        return N1xDlaDims2{
            static_cast<int32_t>(v.arr_val[0].num_val),
            static_cast<int32_t>(v.arr_val[1].num_val),
        };
    }
    if (v.is_number()) {
        int32_t d = static_cast<int32_t>(v.num_val);
        return N1xDlaDims2{d, d};
    }
    return N1xDlaDims2{def_h, def_w};
}

N1xDlaDims4 parse_dims4(const JsonValue &v) {
    if (!v.is_array() || v.arr_val.size() != 4) {
        throw std::runtime_error("expected 4-element shape array [N, C, H, W]");
    }
    return N1xDlaDims4{
        static_cast<int32_t>(v.arr_val[0].num_val),
        static_cast<int32_t>(v.arr_val[1].num_val),
        static_cast<int32_t>(v.arr_val[2].num_val),
        static_cast<int32_t>(v.arr_val[3].num_val),
    };
}

void check_dla(uint32_t status, const char *fn_name) {
    if (status != 0) {
        std::ostringstream oss;
        oss << fn_name << " failed with status " << status;
        throw std::runtime_error(oss.str());
    }
}

N1xDlaWeights parse_weights(const JsonValue &node,
                            const std::string &prefix,
                            uint8_t default_dtype,
                            std::vector<std::vector<uint8_t>> &arena) {
    N1xDlaWeights w{};
    w.dtype = parse_dtype_code(node.at(prefix + "_dtype"), default_dtype);
    w.values = nullptr;
    w.count = 0;

    std::string b64_key = prefix + "_b64";
    if (node.has(b64_key) && node.at(b64_key).is_string()) {
        std::vector<uint8_t> raw = base64_decode(node.at(b64_key).str_val);
        size_t elem_size = 2;
        if (w.dtype == N1X_DLA_DATA_TYPE_FLOAT) elem_size = 4;
        else if (w.dtype == N1X_DLA_DATA_TYPE_HALF || w.dtype == N1X_DLA_DATA_TYPE_INT16 || w.dtype == N1X_DLA_DATA_TYPE_UINT16) elem_size = 2;
        else if (w.dtype == N1X_DLA_DATA_TYPE_INT8 || w.dtype == N1X_DLA_DATA_TYPE_UINT8) elem_size = 1;
        if (raw.size() % elem_size != 0) {
            throw std::runtime_error("base64 weight buffer byte length is not a multiple of element size");
        }
        w.count = static_cast<int64_t>(raw.size() / elem_size);
        if (node.has(prefix + "_count")) {
            w.count = node.int_or(prefix + "_count", w.count);
        }
        arena.push_back(std::move(raw));
        w.values = arena.back().data();
        return w;
    }

    if (node.has(prefix) && node.at(prefix).is_array()) {
        const auto &arr = node.at(prefix).arr_val;
        w.count = static_cast<int64_t>(arr.size());
        if (w.dtype == N1X_DLA_DATA_TYPE_HALF) {
            std::vector<uint8_t> buf(arr.size() * sizeof(uint16_t));
            auto *dst = reinterpret_cast<uint16_t *>(buf.data());
            for (size_t i = 0; i < arr.size(); ++i) {
                dst[i] = fp32_to_fp16(static_cast<float>(arr[i].num_val));
            }
            arena.push_back(std::move(buf));
            w.values = arena.back().data();
        } else if (w.dtype == N1X_DLA_DATA_TYPE_INT8) {
            std::vector<uint8_t> buf(arr.size());
            for (size_t i = 0; i < arr.size(); ++i) {
                int v = static_cast<int>(round(arr[i].num_val));
                if (v < -128) v = -128;
                if (v > 127) v = 127;
                buf[i] = static_cast<uint8_t>(static_cast<int8_t>(v));
            }
            arena.push_back(std::move(buf));
            w.values = arena.back().data();
        } else {
            w.dtype = N1X_DLA_DATA_TYPE_FLOAT;
            std::vector<uint8_t> buf(arr.size() * sizeof(float));
            auto *dst = reinterpret_cast<float *>(buf.data());
            for (size_t i = 0; i < arr.size(); ++i) {
                dst[i] = static_cast<float>(arr[i].num_val);
            }
            arena.push_back(std::move(buf));
            w.values = arena.back().data();
        }
        return w;
    }

    w.dtype = 0;
    return w;
}

N1xDlaWeights make_int32_dims_weights(const JsonValue &v,
                                      int32_t def_val,
                                      std::vector<std::vector<uint8_t>> &arena) {
    std::vector<uint8_t> buf(4 * sizeof(int32_t));
    auto *dst = reinterpret_cast<int32_t *>(buf.data());
    if (v.is_array() && v.arr_val.size() == 4) {
        for (size_t i = 0; i < 4; ++i) {
            dst[i] = static_cast<int32_t>(v.arr_val[i].num_val);
        }
    } else {
        for (size_t i = 0; i < 4; ++i) {
            dst[i] = def_val;
        }
    }
    arena.push_back(std::move(buf));
    N1xDlaWeights w{};
    w.dtype = 7; /* INT32 in NvDla DataType */
    w.values = arena.back().data();
    w.count = 4;
    return w;
}

void apply_tensor_calibration(N1xDlaHandle tensor, const JsonValue &spec, bool is_int8, float default_range) {
    if (spec.has("channel_dynamic_ranges") && spec.at("channel_dynamic_ranges").is_array()) {
        const auto &ranges = spec.at("channel_dynamic_ranges").arr_val;
        for (size_t c = 0; c < ranges.size(); ++c) {
            if (ranges[c].is_array() && ranges[c].arr_val.size() >= 2) {
                float rmin = static_cast<float>(ranges[c].arr_val[0].num_val);
                float rmax = static_cast<float>(ranges[c].arr_val[1].num_val);
                check_dla(NvDlaTensorSetChannelDynamicRange(tensor, static_cast<int32_t>(c), rmin, rmax),
                          "NvDlaTensorSetChannelDynamicRange");
            }
        }
        return;
    }
    if (spec.has("dynamic_range") && spec.at("dynamic_range").is_array() &&
        spec.at("dynamic_range").arr_val.size() >= 2) {
        float rmin = static_cast<float>(spec.at("dynamic_range").arr_val[0].num_val);
        float rmax = static_cast<float>(spec.at("dynamic_range").arr_val[1].num_val);
        check_dla(NvDlaTensorSetChannelDynamicRange(tensor, -1, rmin, rmax),
                  "NvDlaTensorSetChannelDynamicRange");
        if (spec.has("offset") && spec.num_or("offset", 0.0) != 0.0) {
            NvDlaTensorSetChannelOffset(tensor, -1, static_cast<float>(spec.num_or("offset", 0.0)));
        }
        return;
    }
    if (spec.has("scale") && spec.at("scale").is_number()) {
        float scale = static_cast<float>(spec.at("scale").num_val);
        float rmax = 127.0f * scale;
        check_dla(NvDlaTensorSetChannelDynamicRange(tensor, -1, -rmax, rmax),
                  "NvDlaTensorSetChannelDynamicRange");
        if (spec.has("offset") && spec.num_or("offset", 0.0) != 0.0) {
            NvDlaTensorSetChannelOffset(tensor, -1, static_cast<float>(spec.num_or("offset", 0.0)));
        }
        return;
    }
    if (is_int8 && default_range > 0.0f) {
        check_dla(NvDlaTensorSetChannelDynamicRange(tensor, -1, -default_range, default_range),
                  "NvDlaTensorSetChannelDynamicRange");
    }
}

} /* namespace */

extern "C" int n1x_compile_json_graph(const char *json_spec,
                                      uint8_t **out_data,
                                      size_t *out_size,
                                      char *err_buf,
                                      size_t err_buf_size) {
    if (!json_spec || !out_data || !out_size) {
        if (err_buf && err_buf_size > 0) {
            snprintf(err_buf, err_buf_size, "invalid NULL argument to n1x_compile_json_graph");
        }
        return -1;
    }
    *out_data = nullptr;
    *out_size = 0;

    if (n1x_compiler_init(nullptr, nullptr) != 0) {
        if (err_buf && err_buf_size > 0) {
            snprintf(err_buf, err_buf_size, "failed to initialize N1X t254 compiler shim");
        }
        return -2;
    }

    N1xDlaHandle network = nullptr;
    N1xDlaHandle wisdom = nullptr;
    try {
        JsonParser parser(json_spec);
        JsonValue root = parser.parse();
        if (!root.is_object()) {
            throw std::runtime_error("root JSON value must be an object");
        }

        std::string target_name = root.str_or("target", "t254");
        std::string profile_name = root.str_or("profile_name", "probe");
        uint8_t compute_precision = parse_dtype_code(root.at("compute_precision"), N1X_DLA_DATA_TYPE_HALF);
        bool explicit_qdq = root.bool_or("explicit_qdq", false);
        bool is_int8 = (compute_precision == N1X_DLA_DATA_TYPE_INT8);
        uint8_t default_io_dtype = explicit_qdq ? static_cast<uint8_t>(N1X_DLA_DATA_TYPE_HALF) : compute_precision;
        float default_dyn_range = static_cast<float>(root.num_or("default_dynamic_range", 2.0));
        uint8_t default_weight_dtype = (is_int8 || explicit_qdq) ? N1X_DLA_DATA_TYPE_FLOAT : N1X_DLA_DATA_TYPE_HALF;
        uint8_t input_data_format = parse_format_code(root.at("input_data_format"), N1X_DLA_DATA_FORMAT_NCxHWx);
        uint8_t output_data_format = parse_format_code(root.at("output_data_format"), N1X_DLA_DATA_FORMAT_NCxHWx);

        check_dla(NvDlaCreateNetwork(&network), "NvDlaCreateNetwork");

        std::vector<std::vector<uint8_t>> weight_arena;
        std::map<std::string, N1xDlaHandle> tensors;
        std::vector<N1xDlaHandle> all_tensors;

        const JsonValue &calib_map = root.at("calibration");

        /* 1. Add network inputs */
        const JsonValue &inputs = root.at("inputs");
        if (!inputs.is_array() || inputs.arr_val.empty()) {
            throw std::runtime_error("JSON graph must specify non-empty 'inputs' array");
        }
        for (const auto &inp_spec : inputs.arr_val) {
            std::string name = inp_spec.str_or("name", "input");
            N1xDlaDims4 dims = parse_dims4(inp_spec.at("shape"));
            N1xDlaHandle t = nullptr;
            check_dla(NvDlaNetworkAddInput(network, name.c_str(), dims, &t), "NvDlaNetworkAddInput");
            tensors[name] = t;
            all_tensors.push_back(t);

            uint8_t io_dtype = parse_dtype_code(inp_spec.at("dtype"), default_io_dtype);
            uint8_t io_fmt = parse_format_code(inp_spec.at("data_format"), input_data_format);
            check_dla(NvDlaTensorSetDataType(t, io_dtype), "NvDlaTensorSetDataType");
            check_dla(NvDlaTensorSetDataFormat(t, io_fmt), "NvDlaTensorSetDataFormat");

            if (is_int8 && !explicit_qdq) {
                const JsonValue &t_calib = calib_map.has(name) ? calib_map.at(name) : inp_spec;
                apply_tensor_calibration(t, t_calib, true, default_dyn_range);
            }
        }

        /* 2. Add DAG operator layers in topological order */
        const JsonValue &layers = root.at("layers");
        if (!layers.is_array() || layers.arr_val.empty()) {
            throw std::runtime_error("JSON graph must specify non-empty 'layers' array");
        }

        auto get_tensor = [&](const std::string &tname) -> N1xDlaHandle {
            auto it = tensors.find(tname);
            if (it == tensors.end()) {
                throw std::runtime_error("unknown upstream tensor referenced by layer: " + tname);
            }
            return it->second;
        };

        auto get_primary_input = [&](const JsonValue &l) -> N1xDlaHandle {
            if (l.has("input") && l.at("input").is_string()) {
                return get_tensor(l.at("input").str_val);
            }
            if (l.has("inputs") && l.at("inputs").is_array() && !l.at("inputs").arr_val.empty()) {
                return get_tensor(l.at("inputs").arr_val[0].str_val);
            }
            throw std::runtime_error("layer missing 'input' or 'inputs'");
        };

        for (size_t idx = 0; idx < layers.arr_val.size(); ++idx) {
            const auto &l = layers.arr_val[idx];
            std::string op = l.str_or("op", l.str_or("type", ""));
            N1xDlaHandle layer = nullptr;

            if (op == "Convolution" || op == "Conv" || op == "Deconvolution" || op == "ConvTranspose") {
                N1xDlaHandle inp = get_primary_input(l);
                int32_t out_channels = static_cast<int32_t>(l.int_or("out_channels", l.int_or("num_outputs", 0)));
                if (out_channels <= 0) {
                    throw std::runtime_error(op + " requires positive 'out_channels'");
                }
                int32_t pad_val = static_cast<int32_t>(l.int_or("pad_val", 0));
                N1xDlaDims2 kernel = parse_dims2(l.at("kernel"), 1, 1);
                N1xDlaDims2 stride = parse_dims2(l.at("stride"), 1, 1);
                N1xDlaDims2 dilation = parse_dims2(l.at("dilation"), 1, 1);
                N1xDlaDims2 tl_pad{0, 0};
                N1xDlaDims2 br_pad{0, 0};
                if (l.has("padding") && l.at("padding").is_array()) {
                    const auto &pa = l.at("padding").arr_val;
                    if (pa.size() == 2) {
                        tl_pad = N1xDlaDims2{static_cast<int32_t>(pa[0].num_val), static_cast<int32_t>(pa[1].num_val)};
                        br_pad = tl_pad;
                    } else if (pa.size() >= 4) {
                        tl_pad = N1xDlaDims2{static_cast<int32_t>(pa[0].num_val), static_cast<int32_t>(pa[1].num_val)};
                        br_pad = N1xDlaDims2{static_cast<int32_t>(pa[2].num_val), static_cast<int32_t>(pa[3].num_val)};
                    }
                } else {
                    tl_pad = parse_dims2(l.at("pad_tl"), 0, 0);
                    br_pad = parse_dims2(l.at("pad_br"), tl_pad.h, tl_pad.w);
                }
                int32_t groups = static_cast<int32_t>(l.int_or("groups", 1));
                N1xDlaWeights kw = parse_weights(l, "weights", default_weight_dtype, weight_arena);
                N1xDlaWeights bw = parse_weights(l, "bias", default_weight_dtype, weight_arena);
                uint8_t bias_mode = parse_bias_mode(l.at("bias_mode"), bw.count > 0);

                if (op == "Deconvolution" || op == "ConvTranspose") {
                    check_dla(NvDlaNetworkAddDeconvolution(
                                  network, inp, out_channels, pad_val,
                                  kernel, tl_pad, br_pad, stride, dilation,
                                  kw, bw, bias_mode, groups, &layer),
                              "NvDlaNetworkAddDeconvolution");
                } else {
                    check_dla(NvDlaNetworkAddConvolution(
                                  network, inp, out_channels, pad_val,
                                  kernel, tl_pad, br_pad, stride, dilation,
                                  kw, bw, bias_mode, groups, &layer),
                              "NvDlaNetworkAddConvolution");
                }
            } else if (op == "FullyConnected" || op == "Gemm") {
                N1xDlaHandle inp = get_primary_input(l);
                N1xDlaDims4 in_dims{1, 1, 1, 1};
                check_dla(NvDlaTensorGetDimensions(inp, &in_dims), "NvDlaTensorGetDimensions");
                int32_t out_channels = static_cast<int32_t>(l.int_or("out_channels", l.int_or("num_outputs", 0)));
                N1xDlaWeights kw = parse_weights(l, "weights", default_weight_dtype, weight_arena);
                N1xDlaWeights bw = parse_weights(l, "bias", default_weight_dtype, weight_arena);
                uint8_t bias_mode = parse_bias_mode(l.at("bias_mode"), bw.count > 0);
                N1xDlaDims2 kernel{in_dims.h > 0 ? in_dims.h : 1, in_dims.w > 0 ? in_dims.w : 1};
                if (l.has("kernel")) {
                    kernel = parse_dims2(l.at("kernel"), kernel.h, kernel.w);
                }
                N1xDlaDims2 zero_pad{0, 0};
                N1xDlaDims2 unit{1, 1};
                N1xDlaDims2 fc_stride{
                    std::max<int32_t>(1, std::min<int32_t>(kernel.h, 8)),
                    std::max<int32_t>(1, std::min<int32_t>(kernel.w, 8)),
                };
                check_dla(NvDlaNetworkAddConvolution(
                              network, inp, out_channels, 0,
                              kernel, zero_pad, zero_pad, fc_stride, unit,
                              kw, bw, bias_mode, 1, &layer),
                          "NvDlaNetworkAddConvolution(FullyConnected)");
            } else if (op == "Activation" || op == "Relu" || op == "Sigmoid" || op == "Tanh" || op == "Clip" || op == "LeakyRelu") {
                N1xDlaHandle inp = get_primary_input(l);
                uint8_t act_type = (op == "Activation")
                    ? parse_activation_type(l.has("mode") ? l.at("mode") : l.at("act_type"))
                    : parse_activation_type(JsonValue{JsonValue::kString, false, 0.0, op, {}, {}});
                if (l.has("alpha") || l.has("alpha_b64") || l.has("beta") || l.has("beta_b64")) {
                    N1xDlaWeights alpha = parse_weights(l, "alpha", N1X_DLA_DATA_TYPE_FLOAT, weight_arena);
                    N1xDlaWeights beta = parse_weights(l, "beta", N1X_DLA_DATA_TYPE_FLOAT, weight_arena);
                    check_dla(NvDlaNetworkAddActivationWeights(network, inp, act_type, alpha, beta, &layer),
                              "NvDlaNetworkAddActivationWeights");
                } else {
                    check_dla(NvDlaNetworkAddActivation(network, inp, act_type, &layer),
                              "NvDlaNetworkAddActivation");
                }
            } else if (op == "Pooling" || op == "MaxPool" || op == "AveragePool") {
                N1xDlaHandle inp = get_primary_input(l);
                uint8_t pool_type = (op == "Pooling")
                    ? parse_pooling_type(l.has("mode") ? l.at("mode") : l.at("pool_type"))
                    : parse_pooling_type(JsonValue{JsonValue::kString, false, 0.0, op, {}, {}});
                N1xDlaDims2 window = parse_dims2(l.has("window") ? l.at("window") : l.at("kernel"), 2, 2);
                N1xDlaDims2 stride = parse_dims2(l.at("stride"), window.h, window.w);
                N1xDlaDims2 tl_pad{0, 0};
                N1xDlaDims2 br_pad{0, 0};
                if (l.has("padding") && l.at("padding").is_array()) {
                    const auto &pa = l.at("padding").arr_val;
                    if (pa.size() == 2) {
                        tl_pad = N1xDlaDims2{static_cast<int32_t>(pa[0].num_val), static_cast<int32_t>(pa[1].num_val)};
                        br_pad = tl_pad;
                    } else if (pa.size() >= 4) {
                        tl_pad = N1xDlaDims2{static_cast<int32_t>(pa[0].num_val), static_cast<int32_t>(pa[1].num_val)};
                        br_pad = N1xDlaDims2{static_cast<int32_t>(pa[2].num_val), static_cast<int32_t>(pa[3].num_val)};
                    }
                } else {
                    tl_pad = parse_dims2(l.at("pad_tl"), 0, 0);
                    br_pad = parse_dims2(l.at("pad_br"), tl_pad.h, tl_pad.w);
                }
                check_dla(NvDlaNetworkAddPooling(network, inp, pool_type, window, stride, tl_pad, br_pad, &layer),
                          "NvDlaNetworkAddPooling");
            } else if (op == "ElementWise" || op == "Add" || op == "Mul" || op == "Sub" || op == "Max" || op == "Min") {
                if (!l.has("inputs") || !l.at("inputs").is_array() || l.at("inputs").arr_val.size() < 2) {
                    throw std::runtime_error(op + " requires 'inputs' array of 2 tensor names");
                }
                N1xDlaHandle in0 = get_tensor(l.at("inputs").arr_val[0].str_val);
                N1xDlaHandle in1 = get_tensor(l.at("inputs").arr_val[1].str_val);
                uint8_t ew_op = (op == "ElementWise")
                    ? parse_ew_op(l.has("mode") ? l.at("mode") : l.at("op_type"))
                    : parse_ew_op(JsonValue{JsonValue::kString, false, 0.0, op, {}, {}});
                check_dla(NvDlaNetworkAddElementWise(network, in0, in1, ew_op, &layer),
                          "NvDlaNetworkAddElementWise");
            } else if (op == "Scale") {
                N1xDlaHandle inp = get_primary_input(l);
                N1xDlaWeights shift = parse_weights(l, "shift", default_weight_dtype, weight_arena);
                N1xDlaWeights scale = parse_weights(l, "scale", default_weight_dtype, weight_arena);
                N1xDlaWeights power = parse_weights(l, "power", default_weight_dtype, weight_arena);
                uint8_t mode = parse_scale_mode(l.at("mode"));
                if (!l.has("mode") && scale.count == 1) {
                    mode = N1X_DLA_SCALE_UNIFORM;
                }
                check_dla(NvDlaNetworkAddScale(network, inp, mode, shift, scale, power, &layer),
                          "NvDlaNetworkAddScale");
            } else if (op == "BatchNorm" || op == "BatchNormalization") {
                N1xDlaHandle inp = get_primary_input(l);
                uint8_t mode = parse_bn_mode(l.at("mode"));
                N1xDlaWeights mean = parse_weights(l, "mean", default_weight_dtype, weight_arena);
                N1xDlaWeights var = parse_weights(l, "variance", default_weight_dtype, weight_arena);
                float eps = static_cast<float>(l.num_or("epsilon", 1e-5));
                check_dla(NvDlaNetworkAddBatchNorm(network, inp, mode, mean, var, eps, &layer),
                          "NvDlaNetworkAddBatchNorm");
            } else if (op == "Concatenation" || op == "Concat") {
                if (!l.has("inputs") || !l.at("inputs").is_array() || l.at("inputs").arr_val.size() < 2) {
                    throw std::runtime_error("Concatenation requires 'inputs' array of >= 2 tensor names");
                }
                std::vector<N1xDlaHandle> cat_inputs;
                for (const auto &iv : l.at("inputs").arr_val) {
                    cat_inputs.push_back(get_tensor(iv.str_val));
                }
                check_dla(NvDlaNetworkAddConcatenation(
                              network, cat_inputs.data(), static_cast<int32_t>(cat_inputs.size()), &layer),
                          "NvDlaNetworkAddConcatenation");
                if (l.has("axis")) {
                    check_dla(NvDlaConcatenationLayerSetAxis(layer, static_cast<int32_t>(l.int_or("axis", 1))),
                              "NvDlaConcatenationLayerSetAxis");
                }
            } else if (op == "Slice") {
                N1xDlaHandle inp = get_primary_input(l);
                N1xDlaWeights start = make_int32_dims_weights(l.at("start"), 0, weight_arena);
                N1xDlaWeights size = make_int32_dims_weights(l.at("size"), 1, weight_arena);
                N1xDlaWeights stride = make_int32_dims_weights(l.at("stride"), 1, weight_arena);
                N1xDlaWeights fill_val = parse_weights(l, "fill_val", default_weight_dtype, weight_arena);
                uint8_t mode = static_cast<uint8_t>(l.int_or("mode", 0));
                check_dla(NvDlaNetworkAddSlice(network, inp, start, size, stride, fill_val, mode, &layer),
                          "NvDlaNetworkAddSlice");
            } else if (op == "PixelShuffle" || op == "DepthToSpace") {
                N1xDlaHandle inp = get_primary_input(l);
                uint8_t ps_type = static_cast<uint8_t>(l.int_or("pixel_shuffle_type", 0));
                int32_t factor = static_cast<int32_t>(l.int_or("upscale_factor", l.int_or("blocksize", 2)));
                check_dla(NvDlaNetworkAddPixelShuffle(network, inp, ps_type, factor, &layer),
                          "NvDlaNetworkAddPixelShuffle");
            } else if (op == "Resize") {
                N1xDlaHandle inp = get_primary_input(l);
                uint8_t mode = static_cast<uint8_t>(l.int_or("mode", 0));
                if (l.has("mode") && l.at("mode").is_string()) {
                    mode = (l.at("mode").str_val == "linear" || l.at("mode").str_val == "LINEAR") ? 1 : 0;
                }
                if (l.has("output_dims")) {
                    N1xDlaDims4 out_dims = parse_dims4(l.at("output_dims"));
                    uint8_t align = l.bool_or("align_corners", false) ? 1 : 0;
                    check_dla(NvDlaNetworkAddResizeOutputDims(network, inp, mode, out_dims, align, &layer),
                              "NvDlaNetworkAddResizeOutputDims");
                } else {
                    N1xDlaWeights scales = parse_weights(l, "scales", N1X_DLA_DATA_TYPE_FLOAT, weight_arena);
                    check_dla(NvDlaNetworkAddResize(network, inp, mode, scales, &layer),
                              "NvDlaNetworkAddResize");
                }
            } else if (op == "Quantize" || op == "QuantizeLinear") {
                N1xDlaHandle inp = get_primary_input(l);
                N1xDlaWeights scales = parse_weights(l, "scales", N1X_DLA_DATA_TYPE_FLOAT, weight_arena);
                N1xDlaWeights zps = parse_weights(l, "zero_points", N1X_DLA_DATA_TYPE_INT8, weight_arena);
                int32_t axis = static_cast<int32_t>(l.int_or("axis", 1));
                check_dla(NvDlaNetworkAddQuantize(network, inp, scales, zps, axis, &layer),
                          "NvDlaNetworkAddQuantize");
            } else if (op == "Dequantize" || op == "DequantizeLinear") {
                N1xDlaHandle inp = get_primary_input(l);
                N1xDlaWeights scales = parse_weights(l, "scales", N1X_DLA_DATA_TYPE_FLOAT, weight_arena);
                N1xDlaWeights zps = parse_weights(l, "zero_points", N1X_DLA_DATA_TYPE_INT8, weight_arena);
                int32_t axis = static_cast<int32_t>(l.int_or("axis", 1));
                check_dla(NvDlaNetworkAddDequantize(network, inp, scales, zps, axis, &layer),
                          "NvDlaNetworkAddDequantize");
            } else {
                throw std::runtime_error("unsupported layer op: " + op);
            }

            if (l.has("name") && l.at("name").is_string()) {
                check_dla(NvDlaLayerSetName(layer, l.at("name").str_val.c_str()), "NvDlaLayerSetName");
            }
            if (l.has("compute_precision")) {
                uint8_t lp = parse_dtype_code(l.at("compute_precision"), compute_precision);
                check_dla(NvDlaLayerSetComputePrecision(layer, lp), "NvDlaLayerSetComputePrecision");
            }

            N1xDlaHandle out_t = nullptr;
            check_dla(NvDlaLayerGetOutput(layer, 0, &out_t), "NvDlaLayerGetOutput");
            if (op == "Quantize" || op == "QuantizeLinear") {
                check_dla(NvDlaTensorSetDataType(out_t, N1X_DLA_DATA_TYPE_INT8), "NvDlaTensorSetDataType");
            } else if (op == "Dequantize" || op == "DequantizeLinear") {
                check_dla(NvDlaTensorSetDataType(out_t, N1X_DLA_DATA_TYPE_HALF), "NvDlaTensorSetDataType");
            }
            std::string out_key = l.str_or("output", "tensor_" + std::to_string(idx));
            tensors[out_key] = out_t;
            all_tensors.push_back(out_t);

            if (l.has("tensor_name") && l.at("tensor_name").is_string()) {
                check_dla(NvDlaTensorSetName(out_t, l.at("tensor_name").str_val.c_str()), "NvDlaTensorSetName");
            }
            if (is_int8 && !explicit_qdq) {
                const JsonValue &t_calib = calib_map.has(out_key) ? calib_map.at(out_key) : l;
                apply_tensor_calibration(out_t, t_calib, true, default_dyn_range);
            }
        }

        /* 3. Mark network outputs and configure tensor formats */
        const JsonValue &outputs = root.at("outputs");
        if (!outputs.is_array() || outputs.arr_val.empty()) {
            throw std::runtime_error("JSON graph must specify non-empty 'outputs' array");
        }
        std::vector<N1xDlaHandle> output_tensors;
        for (const auto &out_spec : outputs.arr_val) {
            std::string ref_name;
            std::string rename_to = "output";
            uint8_t out_dtype = default_io_dtype;
            uint8_t out_fmt = output_data_format;
            if (out_spec.is_string()) {
                ref_name = out_spec.str_val;
                rename_to = out_spec.str_val;
            } else if (out_spec.is_object()) {
                ref_name = out_spec.str_or("name", "output");
                rename_to = out_spec.str_or("tensor_name", ref_name);
                out_dtype = parse_dtype_code(out_spec.at("dtype"), default_io_dtype);
                out_fmt = parse_format_code(out_spec.at("data_format"), output_data_format);
            } else {
                throw std::runtime_error("each entry in 'outputs' must be a string or object");
            }
            N1xDlaHandle out_t = get_tensor(ref_name);
            check_dla(NvDlaNetworkMarkOutput(network, out_t), "NvDlaNetworkMarkOutput");
            if (!rename_to.empty()) {
                check_dla(NvDlaTensorSetName(out_t, rename_to.c_str()), "NvDlaTensorSetName");
            }
            check_dla(NvDlaTensorSetDataType(out_t, out_dtype), "NvDlaTensorSetDataType");
            check_dla(NvDlaTensorSetDataFormat(out_t, out_fmt), "NvDlaTensorSetDataFormat");
            if (out_spec.is_object() && is_int8 && !explicit_qdq) {
                apply_tensor_calibration(out_t, out_spec, false, 0.0f);
            }
            output_tensors.push_back(out_t);
        }

        /* If set_all_tensor_formats is true (default for pure FP16 to match probe_native_graph.py),
         * set data type and format on all graph tensors.
         */
        bool set_all_formats = root.bool_or("set_all_tensor_formats", !is_int8 && !explicit_qdq);
        if (set_all_formats) {
            for (N1xDlaHandle t : all_tensors) {
                check_dla(NvDlaTensorSetDataType(t, compute_precision), "NvDlaTensorSetDataType");
                check_dla(NvDlaTensorSetDataFormat(t, input_data_format), "NvDlaTensorSetDataFormat");
            }
        }

        /* 4. Create Wisdom, Profile, and Compile */
        uint8_t profile_precision = explicit_qdq ? static_cast<uint8_t>(N1X_DLA_DATA_TYPE_HALF) : compute_precision;
        check_dla(NvDlaCreateWisdom(&wisdom), "NvDlaCreateWisdom");
        std::string wisdom_dir = "/tmp/n1x_wisdom_" + std::to_string(getpid());
        mkdir(wisdom_dir.c_str(), 0755);
        check_dla(NvDlaWisdomOpen(wisdom, wisdom_dir.c_str()), "NvDlaWisdomOpen");
        check_dla(NvDlaWisdomSetDataType(wisdom, profile_precision), "NvDlaWisdomSetDataType");
        check_dla(NvDlaWisdomSetNetworkTransient(wisdom, network), "NvDlaWisdomSetNetworkTransient");

        N1xDlaHandle compiler = nullptr;
        N1xDlaHandle profiler = nullptr;
        N1xDlaHandle profile = nullptr;
        check_dla(NvDlaWisdomGetCompiler(wisdom, &compiler), "NvDlaWisdomGetCompiler");
        check_dla(NvDlaWisdomGetProfiler(wisdom, &profiler), "NvDlaWisdomGetProfiler");
        check_dla(NvDlaProfilerCreateProfile(profiler, profile_name.c_str(), &profile), "NvDlaProfilerCreateProfile");
        check_dla(NvDlaProfileInitWithDefaultProfile(profile), "NvDlaProfileInitWithDefaultProfile");
        check_dla(NvDlaProfileSetComputePrecision(profile, profile_precision), "NvDlaProfileSetComputePrecision");
        check_dla(NvDlaProfileSetNetworkInputDataFormat(profile, input_data_format), "NvDlaProfileSetNetworkInputDataFormat");
        check_dla(NvDlaProfileSetNetworkOutputDataFormat(profile, output_data_format), "NvDlaProfileSetNetworkOutputDataFormat");

        if (explicit_qdq) {
            check_dla(NvDlaProfileSetIsExplicitQuantization(profile, 1), "NvDlaProfileSetIsExplicitQuantization");
        } else if (is_int8) {
            uint8_t qmode = N1X_DLA_QUANTIZATION_PER_FILTER;
            if (root.str_or("quantization_mode", "") == "per_kernel") {
                qmode = N1X_DLA_QUANTIZATION_PER_KERNEL;
            }
            uint8_t smode = N1X_DLA_TENSOR_SCALING_PER_CHANNEL;
            if (root.str_or("tensor_scaling_mode", "") == "per_tensor") {
                smode = N1X_DLA_TENSOR_SCALING_PER_TENSOR;
            }
            check_dla(NvDlaProfileSetQuantizationMode(profile, qmode), "NvDlaProfileSetQuantizationMode");
            check_dla(NvDlaProfileSetTensorScalingMode(profile, smode), "NvDlaProfileSetTensorScalingMode");
        }

        N1xDlaHandle loadable = nullptr;
        check_dla(NvDlaCompilerCompile(compiler, profile_name.c_str(), target_name.c_str(), &loadable),
                  "NvDlaCompilerCompile");

        uint64_t img_size = 0;
        check_dla(NvDlaCompilerGetLoadableImageSize(compiler, profile_name.c_str(), &img_size),
                  "NvDlaCompilerGetLoadableImageSize");
        if (img_size == 0 || img_size > (64ULL << 20)) {
            throw std::runtime_error("invalid compiled loadable size");
        }

        auto *buf = static_cast<uint8_t *>(malloc(static_cast<size_t>(img_size)));
        if (!buf) {
            throw std::runtime_error("malloc failed for loadable buffer");
        }
        check_dla(NvDlaCompilerGetLoadableImage(compiler, profile_name.c_str(), buf),
                  "NvDlaCompilerGetLoadableImage");

        *out_data = buf;
        *out_size = static_cast<size_t>(img_size);

        NvDlaWisdomClose(wisdom);
        NvDlaDestroyWisdom(wisdom);
        NvDlaDestroyNetwork(network);
        return 0;
    } catch (const std::exception &e) {
        if (wisdom) {
            NvDlaDestroyWisdom(wisdom);
        }
        if (network) {
            NvDlaDestroyNetwork(network);
        }
        if (err_buf && err_buf_size > 0) {
            snprintf(err_buf, err_buf_size, "%s", e.what());
        }
        return -3;
    }
}

#ifdef N1X_COMPILE_CLI_MAIN
int main(int argc, char **argv) {
    const char *in_path = nullptr;
    const char *out_path = nullptr;
    for (int i = 1; i < argc; ++i) {
        if (strcmp(argv[i], "-o") == 0 && i + 1 < argc) {
            out_path = argv[++i];
        } else if (!in_path) {
            in_path = argv[i];
        } else {
            fprintf(stderr, "Usage: %s <graph.json|-> -o <output.nvda>\n", argv[0]);
            return 2;
        }
    }
    if (!in_path || !out_path) {
        fprintf(stderr, "Usage: %s <graph.json|-> -o <output.nvda>\n", argv[0]);
        return 2;
    }

    std::string json_str;
    if (strcmp(in_path, "-") == 0) {
        char chunk[4096];
        size_t n = 0;
        while ((n = fread(chunk, 1, sizeof(chunk), stdin)) > 0) {
            json_str.append(chunk, n);
        }
    } else {
        FILE *f = fopen(in_path, "rb");
        if (!f) {
            perror("fopen input");
            return 1;
        }
        char chunk[4096];
        size_t n = 0;
        while ((n = fread(chunk, 1, sizeof(chunk), f)) > 0) {
            json_str.append(chunk, n);
        }
        fclose(f);
    }

    struct timespec t0 {}, t1 {};
    clock_gettime(CLOCK_MONOTONIC, &t0);

    uint8_t *data = nullptr;
    size_t size = 0;
    char err[512] = {};
    int rc = n1x_compile_json_graph(json_str.c_str(), &data, &size, err, sizeof(err));
    if (rc != 0) {
        fprintf(stderr, "n1x-compile error (%d): %s\n", rc, err);
        return 1;
    }

    clock_gettime(CLOCK_MONOTONIC, &t1);
    double elapsed_ms = (t1.tv_sec - t0.tv_sec) * 1e3 + (t1.tv_nsec - t0.tv_nsec) / 1e6;

    FILE *out_f = fopen(out_path, "wb");
    if (!out_f) {
        perror("fopen output");
        n1x_compiler_free_buffer(data);
        return 1;
    }
    if (fwrite(data, 1, size, out_f) != size) {
        perror("fwrite output");
        fclose(out_f);
        n1x_compiler_free_buffer(data);
        return 1;
    }
    fclose(out_f);
    n1x_compiler_free_buffer(data);

    printf("{\"status\":\"ok\",\"output\":\"%s\",\"bytes\":%zu,\"elapsed_ms\":%.2f}\n",
           out_path, size, elapsed_ms);
    return 0;
}
#endif

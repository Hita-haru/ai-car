import serial
import time

ser = serial.Serial(
    port='/dev/serial0',
    baudrate=115200,
    timeout=1
)

ser.reset_input_buffer()
ser.reset_output_buffer()

def main():
    print("起動時のPingを開始します。")
    pingEsp(5)

def pingEsp(count):
    print("---ESP32 ping tool---")
    error = 0
    for i in range(count):
        msg = f"Ping from raspberry pi [{i}]\n"
        ser.write(msg.encode('utf-8'))
        print(f"Message sent [{i}]")
        if ser.in_waiting > 0:
            response = ser.readline().decode('utf-8', errors='ignore').strip()
            print(f"Responce: {response}")
            if response != f"ACK Received data: Ping from raspberry pi [{i}]":
                error += 1
        time.sleep(2)
    print(f"Sent messages: {count} Errors: {error}")

if __name__ == "__main__":
    main()

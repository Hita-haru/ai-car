# gpiozeroを導入してね 
from gpiozero import PWMOutputDevice, DigitalOutputDevice, Servo 
 
 
# GPIOを設定してね 
MOTOR_ENA_PIN =  
MOTOR_IN1_PIN =  
MOTOR_IN2_PIN =  
SERVO_PIN =  
 
 
# DCモーター 
# -255〜255 
# 255:前進 
#0:停止 
#-255:後進 
motor_pwm = PWMOutputDevice(MOTOR_ENA_PIN) 
motor_in1 = DigitalOutputDevice(MOTOR_IN1_PIN) 
motor_in2 = DigitalOutputDevice(MOTOR_IN2_PIN) 
 
def move_motor(speed): 
    speed = max(-255, min(255, speed)) 
    power = abs(speed) / 255.0 
 
    if speed > 0: 
        motor_in1.on() 
        motor_in2.off() 
        motor_pwm.value = power 
    elif speed < 0: 
        motor_in1.off() 
        motor_in2.on() 
        motor_pwm.value = power 
    else: 
        motor_pwm.value = 0 
        motor_in1.off() 
        motor_in2.off() 
 
 
# サーボモーター 
# 0〜180 
# 0: 左  
#90: 中央  
#180: 右 
steering_servo = Servo(SERVO_PIN) 
 
def steer_motor(angle): 
    angle = max(0, min(180, angle)) 
    steering_servo.value = (angle / 90.0) - 1.0 
 
 
# DCモーターを停止,サーボを中央 
def stop_motors(): 
    move_motor(0) 
    steer_motor(90)
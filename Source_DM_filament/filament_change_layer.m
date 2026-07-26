clear
clc


d = 1.75;
r = d/2;
A = pi/4*d^2;

n = 7;
h = 0.125;

cal = zeros(n,4);

for k = 1:7
    theta = acos( 1 - h*k/r );
    cal(k,1) = k;
    cal(k,2) = r^2*theta - r*(r-h*k)*sin(theta);
    cal(k,3) = 2 *r*sin(theta);
    cal(k,4) = cal(k,2)/A*100;
end


    
    